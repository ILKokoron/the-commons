#!/usr/bin/env python3
"""Export a finished The Commons dataset package for one room.

Joins the raw reasoning records (one JSON file per agent per epoch,
written at commit time) with the onchain outcomes (reveals, paid
amounts, pool state) read from the contract, and certifies each joined
row by recomputing the record's sha256 and comparing it with the
reveal's onchain reasoning_hash. A record is looked up first among the
local files in the records dir; when no local file matches a reveal
whose log_ref is a platform store content id (the record's sha256),
the record is fetched from the platform record store instead and
certified exactly the same way. Store access: --store-url /
--store-token flags, else COMMONS_STORE_URL and
COMMONS_STORE_READ_TOKEN (or COMMONS_STORE_TOKEN) from the
environment, else ~/workspace/the-commons/store-credentials/
credentials.env (override with --store-credentials).

Outputs (all amounts as decimal NEAR strings, no yocto anywhere):
  dataset-room<N>.jsonl             certified rows, one per revealed agent-epoch
  dataset-room<N>-unverified.jsonl  records/reveals that could not be certified
                                    (written only when non-empty), each with a reason
  dataset-room<N>-summary.json      room metadata, season totals, per-agent
                                    totals, verification statement
  footprint-room<N>.jsonl           footprint layer for the room: every
                                    game-action attempt (commit, reveal,
                                    settle) with outcome, from the local
                                    footprint JSONL files plus any store
                                    bundles indexed there; the summary
                                    carries footprint_records and
                                    footprint_failed counts

Usage:
  python3 export_dataset.py 1
  python3 export_dataset.py 1 --records-dir PATH --out-dir PATH
"""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONTRACT = "commons.bindingvow1106.testnet"
DEFAULT_RPC = "https://rpc.testnet.fastnear.com"
YOCTO_PER_NEAR = 10 ** 24

BASE = Path(__file__).resolve().parent.parent
DEFAULT_RECORDS = BASE / "records"
DEFAULT_OUT = BASE / "exports"
DEFAULT_STORE_CREDENTIALS = BASE / "store-credentials" / "credentials.env"
DEFAULT_FOOTPRINT_DIR = BASE / "agents" / "logs" / "footprint"
_CONTENT_ID = re.compile(r"^[0-9a-f]{64}$")


def load_store_config(flag_url, flag_token, credentials_path):
    """Resolve (store_url, read_token): flags win, then environment,
    then the platform credentials file. Returns (None, None) when no
    store is configured anywhere."""
    url = flag_url or os.environ.get("COMMONS_STORE_URL", "")
    token = flag_token or os.environ.get("COMMONS_STORE_READ_TOKEN", "") \
        or os.environ.get("COMMONS_STORE_TOKEN", "")
    if (not url or not token) and credentials_path and credentials_path.exists():
        file_cfg = {}
        for line in credentials_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                file_cfg[k.strip()] = v.strip()
        url = url or file_cfg.get("COMMONS_STORE_URL", "")
        token = token or file_cfg.get("COMMONS_STORE_READ_TOKEN", "") \
            or file_cfg.get("COMMONS_STORE_TOKEN", "")
    url = (url or "").rstrip("/")
    return (url, token) if url and token else (None, None)


def fetch_store_record(store_url, token, content_id, attempts=3):
    """GET one record from the platform record store by content id.
    Returns (raw_bytes, None) or (None, error_string)."""
    last = None
    for attempt in range(attempts):
        req = urllib.request.Request(
            f"{store_url}/api/records/{content_id}",
            headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read(), None
        except urllib.error.HTTPError as e:
            return None, f"store returned HTTP {e.code}"
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    return None, f"store fetch failed: {last}"


def _records_from_bundle(raw, room_id):
    """Footprint records out of one store bundle's bytes. A bundle
    is one JSON envelope object with a records list (what the
    runner uploads); a bare JSON object is taken as a single
    record, and anything else is read line by line as JSONL."""
    text = raw.decode(errors="replace")
    try:
        obj = json.loads(text)
    except Exception:
        obj = None
    if isinstance(obj, dict) and isinstance(obj.get("records"), list):
        return [r for r in obj["records"]
                if isinstance(r, dict)
                and int(r.get("room_id", -1)) == room_id]
    if isinstance(obj, dict):
        return [obj] if int(obj.get("room_id", -1)) == room_id else []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict) \
                and int(rec.get("room_id", -1)) == room_id:
            out.append(rec)
    return out


def gather_footprint(room_id, footprint_dir, store_url, store_token):
    """Collect this room's footprint records (every game-action
    attempt: successes and failures). Sources, in order: the local
    agents/logs/footprint/<account>.jsonl files, then any per-epoch
    bundle indexed in footprint/index.json whose (account, epoch)
    has no local records, fetched from the platform record store by
    content id with the same token resolution as reasoning records.
    Returned sorted by timestamp."""
    records = []
    covered = set()
    if not footprint_dir or not footprint_dir.exists():
        return records
    for path in sorted(footprint_dir.glob("*.jsonl")):
        try:
            lines = path.read_text().splitlines()
        except Exception:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if int(rec.get("room_id", -1)) != room_id:
                continue
            records.append(rec)
            covered.add((rec.get("agent"), rec.get("epoch")))
    index_path = footprint_dir / "index.json"
    if store_url and index_path.exists():
        try:
            index = json.loads(index_path.read_text())
        except Exception:
            index = {}
        for key, cid in sorted(index.items()):
            parts = key.split("/", 2)
            if len(parts) != 3:
                continue
            try:
                k_room, k_epoch = int(parts[0]), int(parts[1])
            except Exception:
                continue
            k_agent = parts[2]
            if k_room != room_id or (k_agent, k_epoch) in covered:
                continue
            if not _CONTENT_ID.match(cid or ""):
                continue
            raw, _err = fetch_store_record(store_url, store_token, cid)
            if raw is None:
                continue
            records.extend(_records_from_bundle(raw, room_id))
            covered.add((k_agent, k_epoch))
    records.sort(key=lambda r: (r.get("ts", ""), r.get("agent", ""),
                                r.get("action", "")))
    return records


def yocto_to_near(yocto) -> str:
    """Convert an integer yocto amount (int or decimal string) to a plain
    decimal NEAR string with trailing zeros trimmed. Exact integer math."""
    v = int(yocto)
    sign = "-" if v < 0 else ""
    v = abs(v)
    whole, frac = divmod(v, YOCTO_PER_NEAR)
    if frac == 0:
        return f"{sign}{whole}"
    frac_str = f"{frac:024d}".rstrip("0")
    return f"{sign}{whole}.{frac_str}"


_YOCTO_IN_TEXT = re.compile(r"(\d+)\s*yocto", re.IGNORECASE)


def normalize_reasoning(text: str) -> str:
    """Reasoning text is the agent's verbatim words, except that any embedded
    '<digits> yocto' figure is converted to NEAR so the finished dataset
    carries no yocto figures. The original bytes stay untouched in the
    records store and are certified by reasoning_hash on each row."""
    return _YOCTO_IN_TEXT.sub(lambda m: f"{yocto_to_near(m.group(1))} NEAR", text)


def rpc_view(rpc_url: str, contract: str, method: str, args: dict, attempts: int = 6):
    """Call a view method via curl (the FastNEAR mirror truncated larger
    responses on the Python HTTP path, so shell out to curl and retry)."""
    args_b64 = base64.b64encode(json.dumps(args).encode()).decode()
    body = json.dumps({
        "jsonrpc": "2.0", "id": "1", "method": "query",
        "params": {
            "request_type": "call_function", "finality": "final",
            "account_id": contract, "method_name": method,
            "args_base64": args_b64,
        },
    })
    last_err = None
    for attempt in range(attempts):
        proc = subprocess.run(
            ["curl", "-s", "--max-time", "30", "-X", "POST", rpc_url,
             "-H", "Content-Type: application/json", "-d", body],
            capture_output=True, text=True)
        try:
            payload = json.loads(proc.stdout)
            result = payload["result"]["result"]
            return json.loads(bytes(result).decode())
        except Exception as exc:  # truncated or error response, retry
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"RPC {method} {args} failed after {attempts} tries: {last_err}")


def ns_to_iso(ns) -> str:
    ts = int(ns) / 1e9
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("room_id", type=int)
    ap.add_argument("--records-dir", type=Path, default=DEFAULT_RECORDS)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--contract", default=DEFAULT_CONTRACT)
    ap.add_argument("--rpc", default=DEFAULT_RPC)
    ap.add_argument("--store-url", default=None,
                    help="platform record store base URL (default: env or "
                         "credentials file)")
    ap.add_argument("--store-token", default=None,
                    help="platform record store read token (default: env "
                         "or credentials file)")
    ap.add_argument("--store-credentials", type=Path,
                    default=DEFAULT_STORE_CREDENTIALS)
    ap.add_argument("--footprint-dir", type=Path,
                    default=DEFAULT_FOOTPRINT_DIR,
                    help="footprint JSONL dir (default: agents/logs/"
                         "footprint under the repo)")
    ns = ap.parse_args()
    room_id = ns.room_id
    store_url, store_token = load_store_config(
        ns.store_url, ns.store_token, ns.store_credentials)

    room = rpc_view(ns.rpc, ns.contract, "get_room", {"room_id": room_id})
    if room is None:
        print(f"room {room_id} not found on {ns.contract}", file=sys.stderr)
        return 1
    settled = int(room["next_epoch_to_settle"])

    # Index local records for this room by (epoch, agent), keep raw bytes for hashing.
    records = {}
    for path in sorted(ns.records_dir.glob(f"room{room_id}-epoch*-*.json")):
        raw = path.read_bytes()
        try:
            rec = json.loads(raw.decode())
        except Exception:
            continue
        if int(rec.get("room_id", -1)) != room_id:
            continue
        key = (int(rec["epoch"]), rec["agent"])
        records[key] = {"path": path, "raw": raw, "rec": rec,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "source": "local"}

    certified = []
    unverified = []
    matched_keys = set()
    reveals_seen = 0
    agent_acc = {}  # agent -> yocto totals over certified rows (exact ints)
    total_paid_epochs = 0  # yocto, cross-check against room total_paid_out

    for epoch in range(settled):
        ep = rpc_view(ns.rpc, ns.contract, "get_epoch",
                      {"room_id": room_id, "epoch": epoch})
        if ep is None:
            continue
        total_paid_epochs += int(ep["total_paid"])
        pool_after_near = yocto_to_near(ep["pool_after"])
        for reveal in ep["reveals"]:
            reveals_seen += 1
            agent = reveal["account"]
            key = (epoch, agent)
            entry = records.get(key)
            requested = int(reveal["amount"])
            paid = int(reveal["paid"])
            store_fetch_error = None
            if entry is None:
                # No local file. If the reveal's log_ref is a platform
                # store content id, fetch the record from the store and
                # certify it exactly like a local file.
                log_ref = reveal.get("log_ref", "")
                if _CONTENT_ID.match(log_ref or ""):
                    if store_url:
                        raw, store_fetch_error = fetch_store_record(
                            store_url, store_token, log_ref)
                        if raw is not None:
                            try:
                                srec = json.loads(raw.decode())
                            except Exception:
                                srec = None
                            if srec is not None:
                                entry = {"path": None, "raw": raw,
                                         "rec": srec,
                                         "sha256": hashlib.sha256(raw).hexdigest(),
                                         "source": "store"}
                    else:
                        store_fetch_error = "platform record store is not " \
                            "configured for this exporter run"
            if entry is None:
                log_ref = reveal.get("log_ref", "")
                if _CONTENT_ID.match(log_ref or ""):
                    reason = ("onchain reveal points at platform store "
                              f"record {log_ref} but the record could not "
                              f"be fetched ({store_fetch_error})")
                else:
                    reason = ("onchain reveal has no matching local record "
                              "file (record was never published to the store)")
                unverified.append({
                    "room_id": room_id, "epoch": epoch, "agent": agent,
                    "source": "onchain_reveal",
                    "requested_near": yocto_to_near(requested),
                    "paid_near": yocto_to_near(paid),
                    "reasoning_hash": reveal["reasoning_hash"],
                    "log_ref": log_ref,
                    "hash_verified": False,
                    "reason": reason,
                })
                continue
            matched_keys.add(key)
            rec = entry["rec"]
            if entry["sha256"] != reveal["reasoning_hash"]:
                bad = {
                    "room_id": room_id, "epoch": epoch, "agent": agent,
                    "source": "local_record" if entry["source"] == "local"
                              else "store_record",
                    "requested_near": yocto_to_near(requested),
                    "paid_near": yocto_to_near(paid),
                    "reasoning_hash": reveal["reasoning_hash"],
                    "record_sha256": entry["sha256"],
                    "hash_verified": False,
                    "reason": f"{entry['source']} record sha256 does not "
                              "match the onchain reasoning_hash for this "
                              "reveal",
                }
                if entry["path"] is not None:
                    bad["record_file"] = entry["path"].name
                unverified.append(bad)
                continue
            certified.append({
                "room_id": room_id,
                "epoch": epoch,
                "agent": agent,
                "strategy": rec.get("strategy"),
                "decided_at": rec.get("decided_at"),
                "pool_observed_near": yocto_to_near(rec["pool_observed"]),
                "reserve_observed_near": yocto_to_near(rec["reserve_observed"]),
                "others_last_epoch": [
                    {"agent": o["agent"], "paid_near": yocto_to_near(o["paid"])}
                    for o in rec.get("others_last_epoch", [])
                ],
                "reasoning": normalize_reasoning(rec.get("reasoning", "")),
                "intended_amount_near": yocto_to_near(rec["intended_amount"]),
                "requested_near": yocto_to_near(requested),
                "paid_near": yocto_to_near(paid),
                "capped": requested > paid,
                "pool_after_near": pool_after_near,
                "reasoning_hash": reveal["reasoning_hash"],
                "hash_verified": True,
                "record_source": entry["source"],
            })
            acc = agent_acc.setdefault(agent, {
                "strategy": rec.get("strategy"), "epochs": 0,
                "req": 0, "paid": 0, "capped": 0})
            acc["epochs"] += 1
            acc["req"] += requested
            acc["paid"] += paid
            acc["capped"] += 1 if requested > paid else 0

    # Local records that never matched a reveal: written at commit time but
    # the commit (or the reveal) never landed on chain.
    for key in sorted(records):
        if key in matched_keys:
            continue
        epoch, agent = key
        entry = records[key]
        rec = entry["rec"]
        already = any(u["epoch"] == epoch and u["agent"] == agent
                      and u["source"] == "local_record" for u in unverified)
        if already:
            continue
        unverified.append({
            "room_id": room_id, "epoch": epoch, "agent": agent,
            "source": "local_record",
            "record_file": entry["path"].name,
            "strategy": rec.get("strategy"),
            "decided_at": rec.get("decided_at"),
            "intended_amount_near": yocto_to_near(rec["intended_amount"]),
            "record_sha256": entry["sha256"],
            "hash_verified": False,
            "reason": "record exists in store but no matching onchain reveal "
                      "(commit never landed)",
        })

    certified.sort(key=lambda r: (r["epoch"], r["agent"]))
    unverified.sort(key=lambda r: (r["epoch"], r["agent"]))

    # Per-agent totals over certified rows, summed in yocto ints for exactness.
    per_agent_list = [{
        "agent": agent,
        "strategy": acc["strategy"],
        "epochs_played": acc["epochs"],
        "requested_near": yocto_to_near(acc["req"]),
        "paid_near": yocto_to_near(acc["paid"]),
        "capped_epochs": acc["capped"],
    } for agent, acc in sorted(agent_acc.items())]

    hash_mismatches = sum(1 for u in unverified if "does not match" in u["reason"])

    # Footprint layer: every game-action attempt for this room (the
    # dataset above only carries decisions that landed). Independent
    # of the certified and unverified counts.
    footprint_records = gather_footprint(room_id, ns.footprint_dir,
                                         store_url, store_token)
    footprint_failed = sum(1 for r in footprint_records
                           if r.get("outcome") == "failed")
    summary = {
        "room": {
            "room_id": room_id,
            "name": room["name"],
            "sponsor": room["sponsor"],
            "contract": ns.contract,
            "stake_near": yocto_to_near(room["stake"]),
            "capacity": room["capacity"],
            "cap_bps": room["cap_bps"],
            "cap_percent": room["cap_bps"] / 100,
            "epoch_secs": room["epoch_secs"],
            "season_epochs": room["season_epochs"],
            "refill_per_epoch_near": yocto_to_near(room["refill_per_epoch"]),
            "season_start": ns_to_iso(room["start_ns"]),
            "ended": room["ended"],
        },
        "season_totals": {
            "epochs_settled": settled,
            "total_paid_near": yocto_to_near(room["total_paid_out"]),
            "total_paid_from_epochs_near": yocto_to_near(total_paid_epochs),
            "rows_certified": len(certified),
            "rows_unverified": len(unverified),
        },
        "per_agent": per_agent_list,
        "verification": {
            "method": "sha256 of each raw record's bytes (local file or "
                      "platform store copy fetched by content id) compared "
                      "with the reveal's onchain reasoning_hash",
            "reveals_onchain": reveals_seen,
            "hashes_matched": len(certified),
            "hash_mismatches": hash_mismatches,
            "statement": (
                f"{len(certified)} of {reveals_seen} revealed agent-epochs "
                f"are certified: a record exists whose sha256 equals the "
                f"onchain reasoning_hash. {len(unverified)} item(s) could "
                f"not be certified and are listed separately with reasons."),
        },
    }

    summary["footprint_records"] = len(footprint_records)
    summary["footprint_failed"] = footprint_failed
    summary["footprint_file"] = f"footprint-room{room_id}.jsonl"

    ns.out_dir.mkdir(parents=True, exist_ok=True)
    cert_path = ns.out_dir / f"dataset-room{room_id}.jsonl"
    unver_path = ns.out_dir / f"dataset-room{room_id}-unverified.jsonl"
    summary_path = ns.out_dir / f"dataset-room{room_id}-summary.json"
    footprint_path = ns.out_dir / f"footprint-room{room_id}.jsonl"

    with cert_path.open("w") as fh:
        for row in certified:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    if unverified:
        with unver_path.open("w") as fh:
            for row in unverified:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    elif unver_path.exists():
        unver_path.unlink()

    with summary_path.open("w") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    with footprint_path.open("w") as fh:
        for row in footprint_records:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"room {room_id}: certified={len(certified)} unverified={len(unverified)} "
          f"total_paid={summary['season_totals']['total_paid_near']} NEAR")
    print(f"wrote {cert_path}")
    if unverified:
        print(f"wrote {unver_path}")
    print(f"wrote {summary_path}")
    print(f"footprint: {len(footprint_records)} records "
          f"({footprint_failed} failed) -> {footprint_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
