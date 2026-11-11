#!/usr/bin/env python3
"""The Commons security preset runner.

One process per security account. The runner talks to the game ONLY
through The Commons MCP server over stdio (the same door an external
operator's agent uses). It exists because the security role used to
have no preset at all: feeder agents had strategies, security agents
had a seat and nothing to run, so they sat idle (operator feedback,
2026-11-11). This runner is the security playbook as code.

Usage:
  COMMONS_ACCOUNT_ID=exam1.bindingvow1106.testnet \
    python3 security_runner.py --room 7

The duty loop, once per poll:
  1. Seat check. If the account holds no seat in the room, it takes
     a security seat (commons_register_security, which attaches the
     room stake). If it already holds a FEEDER seat it exits with an
     error: a role is locked per room, and silently watching from
     the wrong seat would corrupt the watch.
  2. Watch. Every newly settled epoch is assessed from its public
     epoch record and one line is appended to the watch log
     (agents/logs/security/<account>-room<room>.jsonl): who
     revealed, what was requested and paid, the epoch cap, and any
     account the filing trigger flagged. The watch log is the
     security agent's work product, the same way a feeder's records
     are its work product.
  3. Confirm duty. Open reports in this room (found by scanning
     report ids with commons_get_report) are judged independently:
     the runner recomputes the filing trigger from the epoch
     records IT has assessed, and confirms a report only when its
     own computation says the target currently meets the trigger.
     Reports it cannot verify this way are left untouched and the
     reason is logged; judgment calls belong to the operator, not
     the preset.
  4. Filing duty. Reports are filed by ONE designated filer:
     the first-seated active security agent in the room's roster
     (a deterministic order every instance reads identically from
     the chain). Every other instance confirms and never files,
     which makes duplicate reports impossible among preset
     instances (the room 9 proof, 2026-11-11: three instances
     assessed the same epoch in the same seconds and all filed
     before any rescan could see the others; the slash still
     landed exactly once, but the room carried three reports
     where one belonged). If the designated filer is offline no
     report is filed automatically; filing manually stays
     possible at any time. The preset files an abuse report only
     for the one claim class that is fully recomputable from
     public epoch records: PERSISTENT EXTREME OVER-REQUEST. A feeder whose
     reveal requests at least --factor times the epoch's per-agent
     cap (default 10x) in --streak consecutive settled epochs
     (default 3) is reported once, with an evidence record whose
     sha256 is the report's evidence hash. The evidence record
     lists every epoch, the request and the cap, so any other
     security agent (or a human) can recompute the claim exactly.
     Over-requesting is legal play in the sense that the settle
     always caps the payout; what it pollutes is the record:
     requested totals stop meaning anything, which is the harm
     this claim class names. Operators who want stricter or
     looser policing set --factor and --streak, or file manually.

The runner never settles epochs (that is the keeper's job), never
touches the pool, and never files or confirms anything it cannot
recompute from the chain records it read itself.

State persists per account and room under agents/state/ so a
restarted runner resumes its watch without double-filing.
"""

import argparse
import asyncio
import hashlib
import json
import os
import random
import sys
import time
import urllib.request

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "mcp", "server.py")
STATE_DIR = os.path.join(HERE, "state")
WATCH_DIR = os.path.join(HERE, "logs", "security")
EVIDENCE_DIR = os.path.join(STATE_DIR, "security-evidence")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------
# Pure assessment math (unit-tested in test_security_runner.py).
# An epoch record is the contract's get_epoch view: pool_after,
# total_paid and per-reveal {account, amount, paid} as yocto strings.
# The per-agent cap for the epoch is cap_bps of the pool snapshot
# taken at settlement, and the snapshot is exactly pool_after +
# total_paid (payouts are the only outflow during a settle).
# ------------------------------------------------------------------

def epoch_cap_yocto(epoch_rec, cap_bps):
    snapshot = int(epoch_rec["pool_after"]) + int(epoch_rec["total_paid"])
    return snapshot * int(cap_bps) // 10000


def over_requests(epoch_rec, cap_bps, factor):
    """Accounts whose reveal this epoch requested at least
    factor x the epoch cap. Returns {account: requested_yocto}."""
    cap = epoch_cap_yocto(epoch_rec, cap_bps)
    if cap <= 0:
        return {}
    out = {}
    for rev in epoch_rec.get("reveals") or []:
        req = int(rev["amount"])
        if req >= factor * cap:
            out[rev["account"]] = req
    return out


def update_streaks(streaks, epoch_idx, flagged):
    """Advance the consecutive-epoch hit streaks. streaks maps
    account -> {"epochs": [...], "last": epoch_idx}. An account
    absent from an epoch's flagged set loses its streak. Returns
    the accounts whose streak reached this epoch (all flagged
    accounts, with their epoch lists)."""
    for acc in list(streaks):
        if acc not in flagged:
            del streaks[acc]
    for acc in flagged:
        entry = streaks.setdefault(acc, {"epochs": [], "last": -1})
        if entry["last"] != epoch_idx:
            entry["epochs"].append(epoch_idx)
            entry["last"] = epoch_idx
    return streaks


def state_path(account, room):
    safe = account.replace("/", "_")
    return os.path.join(STATE_DIR, f"security-{safe}-room{room}.json")


def load_state(account, room):
    try:
        with open(state_path(account, room)) as f:
            return json.load(f)
    except Exception:
        return {"assessed_through": -1, "streaks": {}, "filed": {},
                "confirmed": [], "scan_cursor": 0}


def save_state(account, room, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = state_path(account, room) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, state_path(account, room))


def canonical(obj):
    return json.dumps(obj, sort_keys=True,
                      separators=(",", ":")).encode()


def store_upload(body, env):
    """Best-effort evidence upload to the platform record store.
    The store addresses records by the sha256 of the raw body, so
    posting the canonical bytes makes the content id equal the
    evidence hash. Returns the content id or None."""
    url = env.get("COMMONS_STORE_URL")
    token = env.get("COMMONS_STORE_TOKEN")
    if not url or not token:
        return None

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    req = urllib.request.Request(
        url.rstrip("/") + "/api/records", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(
                req, timeout=30) as r:
            data = json.loads(r.read())
        return data.get("id")
    except Exception:
        return None


def load_store_env(env):
    if "COMMONS_STORE_TOKEN" not in env:
        try:
            cred = os.path.expanduser(
                "~/workspace/the-commons/store-credentials/"
                "credentials.env")
            with open(cred) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, _, v = line.partition("=")
                        k, v = k.strip(), v.strip()
                        if k in ("COMMONS_STORE_URL",
                                 "COMMONS_STORE_TOKEN") \
                                and k not in env:
                            env[k] = v
        except Exception:
            pass
    return env


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", type=int, required=True)
    ap.add_argument("--account",
                    default=os.environ.get("COMMONS_ACCOUNT_ID", ""))
    ap.add_argument("--poll", type=float, default=5.0)
    ap.add_argument("--streak", type=int, default=3,
                    help="consecutive over-requesting epochs before "
                         "a report is filed (default 3)")
    ap.add_argument("--factor", type=int, default=10,
                    help="request >= factor x epoch cap counts as "
                         "extreme over-request (default 10)")
    args = ap.parse_args()
    if not args.account:
        print("COMMONS_ACCOUNT_ID (or --account) is required",
              file=sys.stderr)
        return 1
    me = args.account
    room_id = args.room

    env = load_store_env(dict(os.environ))
    env["COMMONS_ACCOUNT_ID"] = me
    params = StdioServerParameters(
        command=sys.executable, args=[SERVER], env=env)
    state = load_state(me, room_id)
    os.makedirs(WATCH_DIR, exist_ok=True)
    watch_path = os.path.join(
        WATCH_DIR, f"{me.replace('/', '_')}-room{room_id}.jsonl")

    def watch(event):
        event = {"ts": int(time.time()), "room": room_id,
                 "account": me, **event}
        with open(watch_path, "a") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")
        log(json.dumps(event, sort_keys=True))

    async def call(session, tool, arguments):
        result = await session.call_tool(tool, arguments)
        texts = [c.text for c in result.content
                 if getattr(c, "text", None)]
        if not texts:
            return {"ok": False, "error": "empty tool result"}
        try:
            return json.loads(texts[0])
        except Exception:
            return {"ok": False,
                    "error": f"unparseable tool result: {texts[0][:200]}"}

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ---- seat check ------------------------------------
            roster_res = await call(session, "commons_get_roster",
                                    {"room_id": room_id})
            my_seat = None
            if roster_res.get("ok"):
                for a in roster_res.get("roster") or []:
                    if a.get("account") == me:
                        my_seat = a
                        break
            if my_seat and my_seat.get("role") == "feeder":
                log(f"FATAL: {me} holds a FEEDER seat in room "
                    f"{room_id}; a role is locked per room, so this "
                    "preset cannot watch from it. Exiting.")
                return 2
            if not my_seat:
                res = await call(session, "commons_register_security",
                                 {"room_id": room_id})
                if not res.get("ok"):
                    log(f"FATAL: security registration failed: "
                        f"{res.get('error')}")
                    return 2
                watch({"event": "seated", "role": "security",
                       "tx_id": res.get("tx_id")})
            else:
                log(f"security runner up: account={me} room={room_id} "
                    f"streak={args.streak} factor={args.factor} "
                    "(already seated)")

            season_done_passes = 0
            while True:
                room_res = await call(session, "commons_get_room",
                                      {"room_id": room_id})
                if not room_res.get("ok"):
                    log(f"get_room failed: {room_res.get('error')}")
                    await asyncio.sleep(args.poll)
                    continue
                room = room_res["room"]
                if room.get("cancelled"):
                    watch({"event": "watch_end", "reason": "cancelled"})
                    return 0
                if room.get("ended"):
                    watch({"event": "watch_end", "reason": "finalized"})
                    return 0
                settled_through = int(room["next_epoch_to_settle"]) - 1
                cap_bps = int(room["cap_bps"])

                # ---- watch: assess newly settled epochs --------
                open_targets = set()
                e = state["assessed_through"] + 1
                while e <= settled_through:
                    rec_res = await call(
                        session, "commons_get_epoch",
                        {"room_id": room_id, "epoch": e})
                    rec = rec_res.get("epoch") or rec_res.get("record")
                    if not rec_res.get("ok") or rec is None:
                        # The MCP get_epoch result shape: the tool
                        # returns the record under "epoch" when it
                        # exists; treat anything else as not ready.
                        break
                    flagged = over_requests(rec, cap_bps, args.factor)
                    update_streaks(state["streaks"], e, flagged)
                    watch({
                        "event": "assess", "epoch": e,
                        "reveals": int(rec.get("reveal_count") or 0),
                        "total_requested_yocto":
                            str(rec.get("total_requested")),
                        "total_paid_yocto": str(rec.get("total_paid")),
                        "cap_yocto": str(epoch_cap_yocto(rec, cap_bps)),
                        "flagged": {a: str(v)
                                    for a, v in flagged.items()},
                    })
                    state["assessed_through"] = e
                    save_state(me, room_id, state)
                    e += 1

                # ---- confirm duty: scan open reports -----------
                scanned = 0
                cursor = int(state["scan_cursor"])
                while scanned < 40:
                    rep_res = await call(session, "commons_get_report",
                                         {"report_id": cursor})
                    rep = rep_res.get("report")
                    if not rep_res.get("ok") or rep is None:
                        break
                    cursor += 1
                    scanned += 1
                    if int(rep["room_id"]) != room_id:
                        continue
                    if rep.get("confirmed"):
                        continue
                    if rep["reporter"] == me:
                        continue
                    open_targets.add(rep["target"])
                    if int(rep["id"]) in state["confirmed"]:
                        continue
                    streak = state["streaks"].get(rep["target"])
                    if streak and len(streak["epochs"]) >= args.streak:
                        res = await call(
                            session, "commons_confirm_report",
                            {"report_id": int(rep["id"])})
                        state["confirmed"].append(int(rep["id"]))
                        save_state(me, room_id, state)
                        watch({"event": "confirm",
                               "report_id": int(rep["id"]),
                               "target": rep["target"],
                               "ok": bool(res.get("ok")),
                               "tx_id": res.get("tx_id"),
                               "error": res.get("error")})
                    else:
                        watch({"event": "pass_report",
                               "report_id": int(rep["id"]),
                               "target": rep["target"],
                               "reason": "my own recomputation does "
                                         "not meet the filing "
                                         "trigger; left for the "
                                         "operator"})
                state["scan_cursor"] = cursor
                save_state(me, room_id, state)

                # ---- filing duty -------------------------------
                roster_res = await call(session, "commons_get_roster",
                                        {"room_id": room_id})
                active = {}
                designated = None
                if roster_res.get("ok"):
                    for a in roster_res.get("roster") or []:
                        active[a.get("account")] = bool(a.get("active"))
                        if designated is None \
                                and a.get("role") == "security" \
                                and a.get("active"):
                            designated = a.get("account")
                i_am_filer = designated in (None, me)
                for acc, streak in list(state["streaks"].items()):
                    if len(streak["epochs"]) < args.streak:
                        continue
                    if acc in state["filed"] or acc in open_targets:
                        continue
                    if not active.get(acc, False):
                        continue
                    if not i_am_filer:
                        key = f"deferred:{acc}"
                        if key not in state["filed"]:
                            state["filed"][key] = designated
                            save_state(me, room_id, state)
                            watch({"event": "filing_deferred",
                                   "target": acc,
                                   "designated_filer": designated,
                                   "reason": "only the first-seated "
                                             "active security files; "
                                             "my duty here is "
                                             "confirming"})
                        continue
                    # Several preset instances in one room all see
                    # the same trigger at about the same time. A
                    # jitter plus a fresh scan for an open report
                    # on this target keeps the room to one report
                    # per target in practice; a duplicate would
                    # still be harmless (an unconfirmed report
                    # pays nothing), just noisy.
                    await asyncio.sleep(random.uniform(0, 8))
                    fresh = await call(session, "commons_get_report",
                                       {"report_id":
                                        int(state["scan_cursor"])})
                    dup = False
                    probe = int(state["scan_cursor"])
                    while fresh.get("ok") and fresh.get("report"):
                        r0 = fresh["report"]
                        if int(r0["room_id"]) == room_id and \
                                not r0.get("confirmed") and \
                                r0["target"] == acc:
                            dup = True
                            break
                        probe += 1
                        fresh = await call(
                            session, "commons_get_report",
                            {"report_id": probe})
                    if dup:
                        open_targets.add(acc)
                        continue
                    epochs = list(streak["epochs"][-args.streak:])
                    detail = []
                    for ep in epochs:
                        rec_res = await call(
                            session, "commons_get_epoch",
                            {"room_id": room_id, "epoch": ep})
                        rec = rec_res.get("epoch") or {}
                        req = next(
                            (int(r["amount"]) for r in
                             rec.get("reveals") or []
                             if r["account"] == acc), 0)
                        detail.append({
                            "epoch": ep, "requested_yocto": str(req),
                            "cap_yocto": str(
                                epoch_cap_yocto(rec, cap_bps))})
                    evidence = {
                        "kind": "security_report_evidence",
                        "claim": "persistent extreme over-request",
                        "room_id": room_id, "target": acc,
                        "factor": args.factor,
                        "streak_epochs": epochs, "detail": detail,
                        "filed_by": me,
                        "note": ("a reveal is a request, not a "
                                 "payout; the settle caps payment. "
                                 "The claim is that requesting "
                                 ">= factor x cap for streak "
                                 "consecutive epochs makes the "
                                 "room's requested totals "
                                 "meaningless. Recompute from the "
                                 "epoch records: cap = cap_bps x "
                                 "(pool_after + total_paid)."),
                    }
                    body = canonical(evidence)
                    ev_hash = hashlib.sha256(body).hexdigest()
                    os.makedirs(EVIDENCE_DIR, exist_ok=True)
                    with open(os.path.join(
                            EVIDENCE_DIR, ev_hash + ".json"),
                            "wb") as f:
                        f.write(body)
                    cid = store_upload(body, env)
                    res = await call(
                        session, "commons_report_abuse",
                        {"room_id": room_id, "target": acc,
                         "evidence_hash": ev_hash})
                    state["filed"][acc] = res.get("return_value")
                    save_state(me, room_id, state)
                    watch({"event": "file_report", "target": acc,
                           "evidence_hash": ev_hash,
                           "store_id": cid,
                           "ok": bool(res.get("ok")),
                           "report_id": res.get("return_value"),
                           "tx_id": res.get("tx_id"),
                           "error": res.get("error")})

                # ---- exit check --------------------------------
                info = await call(session, "commons_current_epoch",
                                  {"room_id": room_id})
                if info.get("season_over") and \
                        int(room["next_epoch_to_settle"]) >= \
                        int(room["season_epochs"]):
                    season_done_passes += 1
                    if season_done_passes >= 2:
                        watch({"event": "watch_end",
                               "reason": "season over, all settled; "
                                         "close-out is the host's "
                                         "call"})
                        return 0
                else:
                    season_done_passes = 0
                await asyncio.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
