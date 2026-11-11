#!/usr/bin/env python3
"""Reconstruct footprint records from the EXISTING text runner logs.

Before the footprint layer existed, every attempt a demo runner made
already landed in its text log (agents/logs/<account>-room<N>.log),
just unstructured. This script parses those lines back into the
footprint schema (same fields agents/footprint.py writes):

  [HH:MM:SS] epoch N: committed amount=<yocto> tx=<id>
      -> commit, success, tx_id
  [HH:MM:SS] epoch N: revealed tx=<id>
      -> reveal, success, tx_id
  [HH:MM:SS] settled epoch N: total_paid=... pool_after=...
      -> settle, success, tx_id null (the text line carries no tx)
  [HH:MM:SS] epoch N: commit failed: <error>
      -> commit, failed, error
  [HH:MM:SS] epoch N: reveal failed: <error>
      -> reveal, failed, error
  [HH:MM:SS] settle epoch N not done by me: <error>
      -> settle, failed, error

Log timestamps are [HH:MM:SS] in the fleet host's local time
(Asia/Jakarta, override with COMMONS_LOG_TZ) with no date; the
room 2 logs are all from 2026-11-08, so the date is a parameter
(default 2026-11-08) and ts is reconstructed in UTC. The agent for a file
comes from the fleet table in agents/fleet.py (log file name to
account), with a filename-derived fallback.

Usage:
  python3 reconstruct_footprint.py 2 23 38
  python3 reconstruct_footprint.py 2 23 38 --out PATH --logs-dir PATH
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DEFAULT_LOGS = BASE / "agents" / "logs"
DEFAULT_OUT_DIR = BASE / "exports"
DEFAULT_DATE = "2026-11-08"

FLEET_TZ = os.environ.get("COMMONS_LOG_TZ", "Asia/Jakarta")


def _ts_utc(date, hhmmss):
    """A fleet log time (local to the fleet host) as a UTC stamp."""
    dt = datetime.strptime(f"{date} {hhmmss}", "%Y-%m-%d %H:%M:%S")
    dt = dt.replace(tzinfo=ZoneInfo(FLEET_TZ))
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


LINE_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\] (.*)$")
COMMIT_OK = re.compile(r"^epoch (\d+): committed amount=\S+ tx=(\S+)")
REVEAL_OK = re.compile(r"^epoch (\d+): revealed tx=(\S+)")
SETTLE_OK = re.compile(r"^settled epoch (\d+):")
COMMIT_FAIL = re.compile(r"^epoch (\d+): commit failed: (.*)$")
REVEAL_FAIL = re.compile(r"^epoch (\d+): reveal failed: (.*)$")
SETTLE_FAIL = re.compile(r"^settle epoch (\d+) not done by me: (.*)$")


def account_map(room_id, logs_dir):
    """Log file name -> account id, from the fleet table when the
    room matches, else derived from the file name."""
    mapping = {}
    try:
        sys.path.insert(0, str(BASE / "agents"))
        import fleet
        if fleet.ROOM == room_id:
            for account, _strategy, logname in fleet.FLEET:
                mapping[logname] = account
    except Exception:
        pass
    suffix = f"-room{room_id}.log"
    for path in logs_dir.glob(f"*{suffix}"):
        if path.name in mapping:
            continue
        stem = path.name[: -len(suffix)]
        # fleet's judge1 log drops the parent suffix; restore it
        if stem == "judge1":
            stem = "judge1.bindingvow1106.testnet"
        mapping[path.name] = stem
    return mapping


def parse_file(path, account, room_id, date, lo, hi, out):
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return
    for line in lines:
        m = LINE_RE.match(line)
        if not m:
            continue
        hhmmss, rest = m.groups()
        rec = None
        cm = COMMIT_OK.match(rest)
        rm = REVEAL_OK.match(rest)
        sm = SETTLE_OK.match(rest)
        cf = COMMIT_FAIL.match(rest)
        rf = REVEAL_FAIL.match(rest)
        sf = SETTLE_FAIL.match(rest)
        if cm:
            rec = (int(cm.group(1)), "commit", "success",
                   cm.group(2), None)
        elif rm:
            rec = (int(rm.group(1)), "reveal", "success",
                   rm.group(2), None)
        elif sm:
            rec = (int(sm.group(1)), "settle", "success", None, None)
        elif cf:
            rec = (int(cf.group(1)), "commit", "failed", None,
                   cf.group(2)[:500])
        elif rf:
            rec = (int(rf.group(1)), "reveal", "failed", None,
                   rf.group(2)[:500])
        elif sf:
            rec = (int(sf.group(1)), "settle", "failed", None,
                   sf.group(2)[:500])
        if rec is None:
            continue
        epoch, action, outcome, tx_id, error = rec
        if epoch < lo or epoch > hi:
            continue
        out.append({
            "ts": _ts_utc(date, hhmmss),
            "agent": account,
            "room_id": room_id,
            "epoch": epoch,
            "action": action,
            "outcome": outcome,
            "tx_id": tx_id,
            "error": error,
        })


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("room_id", type=int)
    ap.add_argument("epoch_from", type=int)
    ap.add_argument("epoch_to", type=int)
    ap.add_argument("--logs-dir", type=Path, default=DEFAULT_LOGS)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--date", default=DEFAULT_DATE,
                    help="date of the logs, YYYY-MM-DD (default "
                         "2026-11-08, the room 2 logs' day)")
    ns = ap.parse_args()
    out_path = ns.out or (DEFAULT_OUT_DIR /
                          f"footprint-room{ns.room_id}-epochs"
                          f"{ns.epoch_from}-{ns.epoch_to}.jsonl")

    mapping = account_map(ns.room_id, ns.logs_dir)
    records = []
    for logname, account in sorted(mapping.items()):
        parse_file(ns.logs_dir / logname, account, ns.room_id,
                   ns.date, ns.epoch_from, ns.epoch_to, records)
    records.sort(key=lambda r: (r["ts"], r["agent"], r["action"]))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    by_outcome = Counter(r["outcome"] for r in records)
    by_action = Counter(r["action"] for r in records)
    by_agent = Counter(r["agent"] for r in records)
    print(f"room {ns.room_id} epochs {ns.epoch_from}-{ns.epoch_to}: "
          f"{len(records)} footprint records -> {out_path}")
    print(f"outcomes: {dict(sorted(by_outcome.items()))}")
    print(f"actions: {dict(sorted(by_action.items()))}")
    print("per account:")
    for agent, count in sorted(by_agent.items()):
        fails = sum(1 for r in records
                    if r["agent"] == agent and r["outcome"] == "failed")
        print(f"  {agent}: {count} ({fails} failed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
