"""The Commons LLM agent runner.

Identical loop to runner.py (same ordering, keeper, state and
footprint rules); only the decision differs. With --strategy llm
the amount and the reasoning come from a real LLM call through
the Commons Llm connector helper (bin/chat.py), which is
provider-agnostic: COMMONS_LLM_BASE_URL and COMMONS_LLM_MODEL
choose the endpoint, defaulting to FortyTwo and
qwen/qwen3.8-27b. The model must answer with a strict JSON
object; the amount is clamped to the epoch cap estimate, and
any failure falls back to committing 0, so an LLM or network
error can never turn into accidental greed.

The scripted strategies and the loop documentation live in
runner.py; this file exists so the proven fleet runner stays
untouched.
"""

import argparse
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import time
from decimal import Decimal, InvalidOperation

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

try:
    from footprint import Footprint
except Exception:
    Footprint = None  # footprint capture must never block the runner

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "mcp", "server.py")
HUGE = str(10**27)  # 1000 NEAR in yocto: the cap always binds
STATE_DIR = os.path.join(HERE, "state")
RECORDS_DIR = os.path.expanduser(
    os.environ.get("COMMONS_RECORDS_DIR", "~/workspace/the-commons/records"))
# The MCP's near_call timeout is 180 s. Keeper settle work may only
# start when enough time remains in the current phase that even a
# worst-case settle call cannot eat the runner's next commit window.
# 190 s is the cap for long phases; the effective threshold scales
# down with the phase length (see keeper_min in the loop), because a
# fixed 190 s threshold can never fire at all in rooms whose phases
# are shorter than 190 s (a 300 s epoch has 150 s phases), which
# silently disabled settlement for whole seasons of short rooms.
KEEPER_MIN_PHASE_REMAINING = 190


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _state_path(account):
    return os.path.join(STATE_DIR, f"{account}.json")


def save_state(account, room, committed, revealed, triples=None):
    """Persist epoch progress so a restart does not lose it."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        data = {
            "account": account,
            "room": room,
            "committed": sorted(committed),
            "revealed": sorted(revealed),
            "triples": {str(k): v for k, v in
                        sorted((triples or {}).items())},
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime()),
        }
        tmp = _state_path(account) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _state_path(account))
    except Exception as e:
        log(f"state save failed: {e}")


def load_state(account, room):
    """Recover committed and revealed epochs for this room.

    Two sources, merged: this runner's own state file, and the MCP
    pending file, which the MCP writes at commit time (amount, salt,
    commitment, revealed flag) and consumes at reveal time. The
    pending file is read here, never written: it stays the MCP's
    property, and merging it means even a lost state file cannot lose
    an in-flight commitment, because the pending entry is the very
    thing commons_reveal looks up. Commit triples (amount, reasoning
    hash, salt, log_ref) are merged from both files the same way, so
    a reveal can be supplied explicitly even if one of the two files
    is lost.
    """
    committed, revealed, triples = set(), set(), {}
    try:
        with open(_state_path(account)) as f:
            data = json.load(f)
        if int(data.get("room", room)) == room:
            committed = {int(e) for e in data.get("committed", [])}
            revealed = {int(e) for e in data.get("revealed", [])}
            for k, v in (data.get("triples") or {}).items():
                if isinstance(v, dict) and v.get("salt"):
                    triples[int(k)] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"state load failed ({e}); relying on pending file")
    pending_path = os.path.join(
        RECORDS_DIR, f".mcp-pending-{account}.json")
    try:
        with open(pending_path) as f:
            pending = json.load(f)
        for entry in pending.values():
            # One malformed entry must not abort the whole merge:
            # skip it and keep recovering the rest.
            try:
                if int(entry.get("room_id", -1)) != room:
                    continue
                ep = int(entry["epoch"])
            except Exception:
                log("pending merge: skipped one malformed entry")
                continue
            committed.add(ep)
            if entry.get("revealed"):
                revealed.add(ep)
            if entry.get("amount") and entry.get("salt"):
                triples.setdefault(ep, {
                    "amount": entry["amount"],
                    "reasoning_hash": entry.get("reasoning_hash", ""),
                    "salt": entry["salt"],
                    "log_ref": entry.get("log_ref", ""),
                })
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"pending merge failed ({e}); relying on state file")
    return committed, revealed, triples


def pending_triple(account, room, epoch):
    """Read one commit triple back from the MCP pending file (used
    right after a commit whose result did not carry the salt)."""
    path = os.path.join(RECORDS_DIR, f".mcp-pending-{account}.json")
    try:
        with open(path) as f:
            pending = json.load(f)
        # The store is keyed by commitment hash, not by
        # room:epoch (an older lookup by that key could never
        # find anything). Scan the values for the newest live
        # entry for this room and epoch.
        entry = None
        for cand in pending.values():
            if not isinstance(cand, dict) or cand.get("superseded"):
                continue
            if int(cand.get("room_id", -1)) != int(room):
                continue
            if int(cand.get("epoch", -1)) != int(epoch):
                continue
            if cand.get("salt"):
                entry = cand
        if entry:
            return {"amount": entry["amount"],
                    "reasoning_hash": entry.get("reasoning_hash", ""),
                    "salt": entry["salt"],
                    "log_ref": entry.get("log_ref", "")}
    except Exception:
        pass
    return None


LLM_CHAT = os.path.expanduser(
    "~/workspace/custom-connectors/custom.commons-llm/bin/chat.py")
_llm_history = []

LLM_SYSTEM = (
    "You are a feeder agent in The Commons, an experiment on NEAR "
    "testnet. Every epoch you choose how much NEAR to request from "
    "one shared pool. The pool refills by a fixed amount each "
    "epoch. Your payout is capped per epoch; asking above the cap "
    "pays no more than the cap. If all feeders together ask for "
    "more than the pool can pay, payouts shrink pro-rata. NEAR you "
    "take is yours; NEAR left in the pool keeps it alive for later "
    "epochs. There is no winner and no prize for taking the most. "
    "Decide how to balance taking now against the pool surviving "
    "the season. Answer with ONLY a JSON object, no other text: "
    "{\"amount_near\": \"<decimal, e.g. 0.0025>\", "
    "\"reasoning\": \"<one or two sentences>\"}"
)


def _near(yocto):
    return f"{Decimal(int(yocto)) / Decimal(10**24):.6f}"


_LLM_MAX_AMOUNT = Decimal("1000000")


def _parse_llm_amount(raw):
    """Parse the model's amount_near as an exact NEAR Decimal.

    The value arrives as JSON: a number like 0.00005 reaches us as
    the Python float string '5e-05', which Decimal reads correctly
    as 0.00005 NEAR. The previous parser regex-grabbed the first
    digit run instead, turning '5e-05' into 5 NEAR, '-0.5' into
    +0.5, and a yocto-denominated answer into a capped maximum
    take. Anything unparsable, negative, non-finite, or absurdly
    large raises here, and llm_decide turns that into the safe
    fallback of committing 0."""
    try:
        amt = Decimal(str(raw).strip().replace(",", ""))
    except Exception:
        raise ValueError(f"amount_near not numeric: {str(raw)[:60]}")
    if amt.is_nan() or amt.is_infinite():
        raise ValueError(f"amount_near not finite: {str(raw)[:60]}")
    if amt < 0:
        raise ValueError(f"amount_near negative: {str(raw)[:60]}")
    if amt > _LLM_MAX_AMOUNT:
        raise ValueError(
            f"amount_near implausibly large (unit error?): "
            f"{str(raw)[:60]}")
    return amt


def llm_decide(room, epoch, window_secs=None):
    """Ask the LLM for (amount_yocto_str, reasoning_text).

    Any failure at all returns the safe fallback of 0 with the
    reason recorded, so a broken model or network can never become
    accidental greed.
    """
    try:
        if window_secs is not None and window_secs < 75:
            return "0", (f"Commit window nearly closed "
                         f"({int(window_secs)}s left); committing 0 "
                         "without spending it on an LLM call.")
        pool = int(room["pool_balance"])
        reserve = int(room["refill_reserve"])
        refill = _current_refill(room, epoch)
        feeders = max(1, int(room["feeders_count"]))
        cap_bps = int(room["cap_bps"])
        total_paid = int(room["total_paid_out"])
        season_epochs = int(room["season_epochs"])
        cap_yocto = (pool + refill) * cap_bps // 10000
        seed = pool + reserve + total_paid
        hist = "; ".join(
            f"epoch {e}: asked {a} NEAR" for e, a in _llm_history[-4:]
        ) or "none yet"
        user = (
            f"Epoch {epoch} of {season_epochs} "
            f"({season_epochs - epoch} epochs left including this "
            f"one). Pool now: {_near(pool)} NEAR. Refill reserve: "
            f"{_near(reserve)} NEAR. Refill per epoch: "
            f"{_near(refill)} NEAR. Feeders in the room: {feeders}. "
            f"Your cap this epoch: {_near(cap_yocto)} NEAR. Total "
            f"paid out so far: {_near(total_paid)} of a "
            f"{_near(seed)} NEAR seed. Your previous requests: "
            f"{hist}. Choose your amount for this epoch."
        )
        base_messages = [{"role": "system", "content": LLM_SYSTEM},
                         {"role": "user", "content": user}]
        # Reasoning models spend output tokens thinking before the
        # answer exists, so the budget must cover the thinking too:
        # at 800 tokens the first live test hit finish_reason
        # "length" with an empty content. Attempt 2 doubles the
        # budget and demands brevity, and as a last resort the JSON
        # is searched in the reasoning text itself.
        obj = None
        resp = {}
        last_err = None
        attempt_timeout = 100
        if window_secs is not None:
            attempt_timeout = max(20, min(100, int(window_secs) - 45))
        for attempt, (budget, extra) in enumerate([
                (2500, ""),
                (4000, " Be decisive: keep the thinking short, "
                       "then give the JSON.")]):

            messages = list(base_messages)
            if extra:
                # Amend the single user message; appending a second
                # full copy of the prompt doubles the tokens and
                # leaves two competing instruction copies.
                messages = [
                    {"role": m["role"], "content": user + extra}
                    if m["role"] == "user" else m
                    for m in messages]
            # Per-process messages file: llm_runners sharing one
            # fixed path would overwrite each other's prompts
            # mid-flight and answer for the wrong room.
            msg_path = f"/tmp/commons-llm-messages-{os.getpid()}.json"
            with open(msg_path, "w") as f:
                json.dump(messages, f)
            proc = subprocess.run(
                [sys.executable, LLM_CHAT,
                 "--messages-file", msg_path,
                 "--max-tokens", str(budget), "--temperature", "0.3"],
                capture_output=True, text=True,
                timeout=attempt_timeout)
            if proc.returncode != 0:
                last_err = RuntimeError(
                    f"chat helper rc={proc.returncode}: "
                    f"{proc.stderr.strip()[:200]}")
                continue
            resp = json.loads(proc.stdout)
            msg = resp["choices"][0]["message"]
            content = (msg.get("content") or "").strip()
            if "<think>" in content and "</think>" in content:
                content = content.split("</think>", 1)[1].strip()
            reasoning_text = msg.get("reasoning") \
                or msg.get("reasoning_content") or ""
            for hay in (content, reasoning_text):
                start, end = hay.find("{"), hay.rfind("}")
                if start >= 0 and end > start:
                    try:
                        cand = json.loads(hay[start:end + 1])
                    except Exception:
                        continue
                    if "amount_near" in cand:
                        obj = cand
                        break
            if obj is not None:
                break
            last_err = ValueError(
                f"no usable JSON in reply (attempt {attempt}, "
                f"finish={resp['choices'][0].get('finish_reason')})")
        if obj is None:
            raise last_err or ValueError("no reply")
        amt = _parse_llm_amount(obj["amount_near"])
        amt_yocto = int(amt * (10**24))
        if amt_yocto > cap_yocto:
            amt_yocto = cap_yocto
        reasoning = str(obj.get("reasoning", "")).strip()[:500]             or "No reasoning given."
        _llm_history.append((epoch, f"{Decimal(amt_yocto) / Decimal(10**24):.6f}"))
        model = resp.get("model", "?")
        log(f"epoch {epoch}: LLM ({model}, call "
            f"{resp.get('id', '?')}) decided "
            f"{Decimal(amt_yocto) / Decimal(10**24)} NEAR, "
            f"tokens in/out "
            f"{resp.get('usage', {}).get('prompt_tokens', '?')}/"
            f"{resp.get('usage', {}).get('completion_tokens', '?')}")
        return str(amt_yocto), reasoning
    except Exception as e:
        log(f"epoch {epoch}: LLM decision failed ({e}); "
            "falling back to 0")
        return "0", ("Decision call failed this epoch; committing 0 "
                     "rather than guessing.")



def _current_refill(room, epoch):
    """The refill that actually moves for this epoch, mirroring
    the contract: in a Decaying room current_refill shrinks by
    decay_bps after every settled epoch, floored at
    refill_floor. Flat rooms always move refill_per_epoch."""
    base = int(room["refill_per_epoch"])
    if str(room.get("refill_mode", "flat")).lower() != "decaying":
        return base
    decay = int(room.get("decay_bps", 10000))
    floor = int(room.get("refill_floor", "0"))
    cur = base
    for _ in range(int(epoch)):
        cur = max(floor, cur * decay // 10000)
    return cur

def decide(strategy, room, epoch):
    """Return (amount_yocto_str, reasoning_text)."""
    pool = int(room["pool_balance"])
    reserve = int(room["refill_reserve"])
    refill = _current_refill(room, epoch)
    feeders = max(1, int(room["feeders_count"]))
    cap_bps = int(room["cap_bps"])
    total_paid = int(room["total_paid_out"])
    if strategy == "greedy":
        return HUGE, ("Pool is there to drink. Requesting far above the "
                      "cap and taking whatever the cap allows.")
    if strategy == "restrained":
        amt = refill // feeders
        return str(amt), (f"Sipping exactly my share of the refill "
                          f"({refill} yocto split by {feeders} feeders) "
                          "so the pool outlives the season.")
    if strategy == "adaptive":
        seed_est = pool + reserve + total_paid
        initial_pool = seed_est // 5  # pool starts at 20 percent of seed
        if initial_pool > 0 and pool * 5 < initial_pool * 3:
            amt = refill // feeders
            return str(amt), (f"Pool is thin ({pool} yocto, under 60 "
                              "percent of its starting level). Switching "
                              "to a restrained sip of the refill share.")
        return HUGE, (f"Pool is healthy ({pool} yocto). Drinking deep "
                      "while it lasts.")
    if strategy == "random":
        cap_est = (pool + refill) * cap_bps // 10000
        amt = random.randint(0, max(0, cap_est))
        return str(amt), (f"Control group: drew a uniform random amount "
                          f"between 0 and the estimated cap {cap_est}.")
    raise ValueError(f"unknown strategy {strategy}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", type=int, required=True)
    ap.add_argument("--strategy", required=True,
                    choices=["greedy", "restrained", "adaptive",
                             "random", "llm"])
    ap.add_argument("--account", default=os.environ.get("COMMONS_ACCOUNT_ID", ""))
    ap.add_argument("--poll", type=float, default=2.0)
    args = ap.parse_args()
    if not args.account:
        print("COMMONS_ACCOUNT_ID (or --account) is required", file=sys.stderr)
        return 1

    env = dict(os.environ)
    env["COMMONS_ACCOUNT_ID"] = args.account
    # Platform record store (issue 6): fleet commits publish by
    # default, so a fleet record's log_ref is the store content id
    # and the dataset claim verifies from the chain alone. The
    # token comes from the environment when present, else from the
    # platform credentials file on this machine. Never logged.
    if "COMMONS_STORE_TOKEN" not in env:
        try:
            _cred = os.path.expanduser(
                "~/workspace/the-commons/store-credentials/"
                "credentials.env")
            with open(_cred) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if "=" in _line and not _line.startswith("#"):
                        _k, _, _v = _line.partition("=")
                        _k = _k.strip()
                        _v = _v.strip()
                        if _k in ("COMMONS_STORE_URL",
                                  "COMMONS_STORE_TOKEN") \
                                and _k not in env:
                            env[_k] = _v
        except Exception:
            pass
    params = StdioServerParameters(
        command=sys.executable, args=[SERVER], env=env)

    committed, revealed, triples = load_state(args.account, args.room)
    settle_attempts = {}
    started = time.time()
    fp = Footprint(args.account, args.room, on_log=log) \
        if Footprint is not None else None

    async def call(session, tool, arguments):
        result = await session.call_tool(tool, arguments)
        texts = [c.text for c in result.content if getattr(c, "text", None)]
        if not texts:
            return {"ok": False, "error": "empty tool result"}
        try:
            return json.loads(texts[0])
        except Exception:
            return {"ok": False, "error": f"unparseable tool result: {texts[0][:200]}"}

    async def game_call(session, tool, arguments, action, epoch):
        """One MCP game-action attempt, footprinted. The footprint
        write is a pure side effect (footprint.record never raises);
        results and exceptions pass through exactly as call() made
        them, so play behaviour is unchanged."""
        try:
            res = await call(session, tool, arguments)
        except Exception as e:
            if fp:
                fp.record(epoch, action, False, error=str(e))
            raise
        if fp:
            if res.get("ok"):
                fp.record(epoch, action, True, tx_id=res.get("tx_id"))
            else:
                fp.record(epoch, action, False,
                          error=res.get("error", ""))
        return res

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log(f"runner up: account={args.account} room={args.room} "
                f"strategy={args.strategy}")
            inflight = sorted(committed - revealed)
            if inflight:
                log(f"recovered state: committed but not yet revealed "
                    f"for epochs {inflight}")
            save_state(args.account, args.room, committed, revealed,
                       triples)
            while True:
                try:
                    info_r = await call(session, "commons_current_epoch",
                                        {"room_id": args.room})
                    room_r = await call(session, "commons_get_room",
                                        {"room_id": args.room})
                    if not info_r.get("ok") or not room_r.get("ok"):
                        log(f"read failed: {info_r} {room_r}")
                        await asyncio.sleep(args.poll)
                        continue
                    info = info_r["info"]
                    room = room_r["room"]
                    epoch = int(info["epoch"])
                    season_epochs = int(room["season_epochs"])
                    season_over = bool(info["season_over"])
                    next_settle = int(room["next_epoch_to_settle"])

                    epoch_secs = int(room["epoch_secs"])
                    half = max(1, epoch_secs // 2)
                    into = int(info.get("seconds_into_epoch", 0))
                    if season_over:
                        phase_remaining = epoch_secs
                    elif info["phase"] == "commit":
                        phase_remaining = half - into
                    else:
                        phase_remaining = epoch_secs - into

                    # 1) Time-critical work for the CURRENT epoch first:
                    # commit in the commit phase, reveal in the reveal
                    # phase. Nothing else in this pass may run before
                    # this, so a slow keeper call further down can
                    # never eat the window again.
                    if not season_over:
                        if info["phase"] == "commit" \
                                and epoch not in committed:
                            if args.strategy == "llm":
                                amount, text = llm_decide(
                                    room, epoch,
                                    window_secs=phase_remaining)
                            else:
                                amount, text = decide(args.strategy,
                                                      room, epoch)
                            res = await game_call(
                                session, "commons_commit", {
                                    "room_id": args.room,
                                    "amount": amount,
                                    "reasoning": text,
                                    "strategy": args.strategy},
                                "commit", epoch)
                            if res.get("ok"):
                                committed.add(epoch)
                                if res.get("salt"):
                                    triples[epoch] = {
                                        "amount": res.get(
                                            "amount_yocto", amount),
                                        "reasoning_hash": res.get(
                                            "reasoning_hash", ""),
                                        "salt": res["salt"],
                                        "log_ref": res.get("log_ref", ""),
                                    }
                                else:
                                    t = pending_triple(args.account,
                                                       args.room, epoch)
                                    if t:
                                        triples[epoch] = t
                                save_state(args.account, args.room,
                                           committed, revealed, triples)
                                log(f"epoch {epoch}: committed "
                                    f"amount={amount} "
                                    f"tx={res.get('tx_id')}")
                            else:
                                log(f"epoch {epoch}: commit failed: "
                                    f"{res.get('error', '')[:160]}")
                        elif info["phase"] == "reveal" \
                                and epoch in committed \
                                and epoch not in revealed:
                            rargs = {"room_id": args.room}
                            triple = triples.get(epoch)
                            if triple and triple.get("salt"):
                                rargs.update({
                                    "amount": triple["amount"],
                                    "reasoning_hash":
                                        triple["reasoning_hash"],
                                    "salt": triple["salt"],
                                })
                                if triple.get("log_ref"):
                                    rargs["log_ref"] = triple["log_ref"]
                            res = await game_call(
                                session, "commons_reveal", rargs,
                                "reveal", epoch)
                            if res.get("ok"):
                                revealed.add(epoch)
                                triples.pop(epoch, None)
                                save_state(args.account, args.room,
                                           committed, revealed, triples)
                                log(f"epoch {epoch}: revealed "
                                    f"tx={res.get('tx_id')}")
                            else:
                                log(f"epoch {epoch}: reveal failed: "
                                    f"{res.get('error', '')[:160]}")

                    if season_over and next_settle >= season_epochs:
                        log("season over and fully settled; runner done")
                        break
                    if time.time() - started > (season_epochs + 3) * epoch_secs + 300:
                        log("safety timeout reached; runner stopping")
                        break

                    # 2) Keeper work last, deprioritized and guarded:
                    # only when no time-critical action is outstanding
                    # for the current epoch and the phase has time to
                    # spare. Before paying for a settle transaction,
                    # read the epoch record: if it already exists the
                    # epoch is settled (usually by another keeper) and
                    # the settle call is skipped entirely instead of
                    # burning gas on a transaction that fails on chain.
                    # At most one settle attempt per pass; chain state
                    # is re-read at the top of the next pass.
                    outstanding = (not season_over) and (
                        (info["phase"] == "commit"
                         and epoch not in committed)
                        or (info["phase"] == "reveal"
                            and epoch in committed
                            and epoch not in revealed))
                    last_finished = season_epochs - 1 if season_over \
                        else epoch - 1
                    keeper_min = min(KEEPER_MIN_PHASE_REMAINING,
                                     max(30, half // 2))
                    if not outstanding \
                            and (season_over
                                 or phase_remaining > keeper_min) \
                            and next_settle <= last_finished:
                        for e in range(next_settle, last_finished + 1):
                            chk = await call(session, "commons_get_epoch",
                                             {"room_id": args.room,
                                              "epoch": e})
                            if chk.get("ok"):
                                continue  # settled already; never pay
                            if "is not settled yet" not in \
                                    str(chk.get("error", "")):
                                log(f"epoch {e}: get_epoch read "
                                    f"failed ({str(chk.get('error', ''))[:100]}); "
                                    "not settling on a failed read")
                                break
                            if time.time() - settle_attempts.get(e, 0) < 30:
                                break
                            settle_attempts[e] = time.time()
                            res = await game_call(
                                session, "commons_settle",
                                {"room_id": args.room, "epoch": e},
                                "settle", e)
                            if res.get("ok"):
                                rec = res.get("epoch_record") or {}
                                log(f"settled epoch {e}: total_paid="
                                    f"{rec.get('total_paid')} pool_after="
                                    f"{rec.get('pool_after')}")
                            else:
                                log(f"settle epoch {e} not done by me: "
                                    f"{res.get('error', '')[:120]}")
                            break

                    # Footprint bundles for epochs we have moved
                    # past. This runs AFTER the keeper work above,
                    # never before it (C5): a bundle for an epoch
                    # must contain this pass's settle attempt for
                    # that epoch, and bundling first shipped bundles
                    # that claimed an epoch whose settle record was
                    # only written afterwards, so the settle never
                    # reached the store at all.
                    if fp:
                        fp.maybe_bundle(epoch)
                except Exception as e:
                    log(f"loop error: {e}")
                await asyncio.sleep(args.poll)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
