#!/usr/bin/env python3
"""Fleet watchdog for the room 2 demo runners.

Run every 5 minutes by the fleet-watchdog cron job. Two layers of
health are checked per runner:

1. PROCESS signal (the original check): the pid recorded in the
   pidfile must be alive AND the log file must have advanced within
   the last 15 minutes. Dead or stale processes are relaunched via
   fleet.py, one dated RELAUNCH line per relaunch appended to
   agents/logs/fleet-watchdog.log and printed.

2. PLAYING signal: a runner only counts as actually playing if its
   log holds a successful commit or reveal line carrying a real tx
   id within the last 2 epoch lengths (room 2 epochs are 600 s, so
   1200 s). A runner can be alive and logging failure retries for
   hours while never landing a commit (that is exactly how the
   2026-11-08 room 2 stall hid: LackBalanceForState on the contract
   plus operator accounts under the near CLI upfront balance wall).
   Relaunching does not fix that class of problem, so a non-playing
   runner is NOT relaunched; it is reported with one NOT-PLAYING
   line per runner, plus a dated copy in fleet-watchdog.log.

The run also reads the v1 contract account balance and storage via
RPC and raises ALARM-HEADROOM when the storage headroom drops
under a threshold (default 0.05 NEAR, env COMMONS_HEADROOM_ALARM_NEAR
overrides it). At the measured growth of about 0.00153 NEAR of
storage per settled epoch, 0.05 NEAR of headroom is roughly 32
epochs, about 5 hours, of warning before the contract can no
longer store new commitments.

Summary output: when every runner is playing and no alarm fires,
exactly one PLAYING-OK line is printed. When some runners are not
playing, the NOT-PLAYING lines are followed by one
PLAYING-DEGRADED line. Exit code stays 0 unless the script itself
crashes; RPC failures never crash the run and never fabricate a
headroom number.
"""

import base64
import calendar
import json
import os
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet

STALE_SECS = 15 * 60
WATCHDOG_LOG = os.path.join(fleet.HERE, "logs", "fleet-watchdog.log")
CONTRACT = "commons.bindingvow1106.testnet"
ROOM_ID = 2
FALLBACK_EPOCH_SECS = 600
RPC_URLS = [
    "https://rpc.testnet.fastnear.com",
    "https://rpc.testnet.near.org",
]
DEFAULT_HEADROOM_ALARM_NEAR = 0.05
LOG_TAIL_BYTES = 512 * 1024

# [09:47:36] epoch 42: committed amount=... tx=<base58>
# [09:52:36] epoch 42: revealed tx=<base58>
SUCCESS_RE = re.compile(
    r"^\[(\d{2}):(\d{2}):(\d{2})\] epoch (\d+): (?:committed|revealed)\b"
    r".*\btx=([1-9A-HJ-NP-Za-km-z]{32,})")


def dated(line):
    return (f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
            f"{line}")


def append_watchdog_log(line):
    with open(WATCHDOG_LOG, "a") as f:
        f.write(dated(line) + "\n")


def rpc_query(params):
    """One JSON-RPC query, rotating endpoints. Returns the result
    dict or None on any failure. Never raises."""
    body = json.dumps({"jsonrpc": "2.0", "id": "watchdog",
                       "method": "query", "params": params})
    for url in RPC_URLS:
        try:
            out = subprocess.run(
                ["curl", "-s", "--max-time", "10", "-X", "POST", url,
                 "-H", "Content-Type: application/json", "-d", body],
                capture_output=True, text=True, timeout=15)
            data = json.loads(out.stdout)
            if "result" in data:
                return data["result"]
        except Exception:
            continue
    return None


def get_epoch_secs():
    """Epoch length of room 2 from chain, fallback 600 s."""
    args = base64.b64encode(
        json.dumps({"room_id": ROOM_ID}).encode()).decode()
    result = rpc_query({
        "request_type": "call_function",
        "account_id": CONTRACT,
        "method_name": "get_room",
        "args_base64": args,
        "finality": "final",
    })
    try:
        room = json.loads(bytes(result["result"]).decode())
        secs = int(room["epoch_secs"])
        if secs > 0:
            return secs
    except Exception:
        pass
    return FALLBACK_EPOCH_SECS


def get_headroom_near():
    """Contract storage headroom in NEAR, or None if RPC fails.

    NEAR storage costs 10^19 yocto per byte, so headroom is the
    balance left after paying for the storage already used."""
    result = rpc_query({
        "request_type": "view_account",
        "account_id": CONTRACT,
        "finality": "final",
    })
    try:
        amount = int(result["amount"])
        storage = int(result["storage_usage"])
        return (amount - storage * 10**19) / 10**24
    except Exception:
        return None


def resolve_log_time(hh, mm, ss, now):
    """Resolve an HH:MM:SS UTC log timestamp to an epoch time.

    Log lines carry no date, so assume today UTC; if that lands in
    the future by more than 60 s the line is from yesterday."""
    tm = time.gmtime(now)
    candidate = calendar.timegm(
        (tm.tm_year, tm.tm_mon, tm.tm_mday, hh, mm, ss, 0, 0, 0))
    if candidate - now > 60:
        candidate -= 86400
    return candidate


def last_success(logpath, now=None):
    """Most recent successful commit or reveal in a runner log.

    Returns (age_secs, epoch) where age_secs is an int, or
    (None, None) when the log holds no success line at all. Only
    the tail of the log is read; success lines land at least twice
    per epoch in a healthy runner, so the tail always covers the
    playing threshold window."""
    if now is None:
        now = time.time()
    try:
        size = os.path.getsize(logpath)
        with open(logpath, "rb") as f:
            if size > LOG_TAIL_BYTES:
                f.seek(size - LOG_TAIL_BYTES)
            raw = f.read()
    except OSError:
        return None, None
    lines = raw.decode(errors="replace").splitlines()
    for line in reversed(lines):
        m = SUCCESS_RE.match(line)
        if not m:
            continue
        hh, mm, ss = (int(m.group(i)) for i in (1, 2, 3))
        epoch = int(m.group(4))
        age = int(now - resolve_log_time(hh, mm, ss, now))
        return max(age, 0), epoch
    return None, None


def check_playing(logpath, threshold_secs, now=None):
    """Assess one runner's playing state from its log alone.

    Returns (playing_bool, age_secs_or_None, epoch_or_None)."""
    age, epoch = last_success(logpath, now=now)
    if age is None:
        return False, None, None
    return age <= threshold_secs, age, epoch


def format_not_playing(account, age, epoch, process):
    age_s = "none" if age is None else str(age)
    epoch_s = "unknown" if epoch is None else str(epoch)
    return (f"NOT-PLAYING account={account} "
            f"last_success_age_secs={age_s} "
            f"last_success_epoch={epoch_s} process={process}")


def headroom_alarm_threshold():
    try:
        return float(os.environ.get(
            "COMMONS_HEADROOM_ALARM_NEAR", DEFAULT_HEADROOM_ALARM_NEAR))
    except ValueError:
        return DEFAULT_HEADROOM_ALARM_NEAR


def fmt_near(x):
    return "unknown" if x is None else f"{x:.4f}"


def fmt_threshold(x):
    return ("%f" % x).rstrip("0").rstrip(".")


def check_runner(account, strategy, logname, threshold_secs, now):
    """Run both health layers for one runner.

    Returns (output_lines, playing_bool, last_success_age_or_None).
    Process problems trigger the original relaunch path and
    RELAUNCH lines; the playing verdict comes from the log's last
    success line either way."""
    recorded_pid = fleet.read_pid(account)
    pid = fleet.running_pid(account)
    logpath = fleet.log_path(logname)
    try:
        log_age = time.time() - os.path.getmtime(logpath)
    except OSError:
        log_age = float("inf")
    lines = []
    process = "alive" if pid else "dead"
    if not (pid and log_age <= STALE_SECS):
        reason = ("process dead" if not pid
                  else f"log stale {int(log_age)}s")
        if pid:  # alive but wedged: stop it before relaunching
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(2)
            if fleet.pid_alive(pid, account):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        new_pid, _ = fleet.start_runner(account, strategy, logname)
        line = (f"RELAUNCH account={account} reason=\"{reason}\" "
                f"old_pid={recorded_pid} new_pid={new_pid}")
        append_watchdog_log(line)
        lines.append(dated(line))
    playing, age, epoch = check_playing(logpath, threshold_secs, now=now)
    if not playing:
        line = format_not_playing(account, age, epoch, process)
        append_watchdog_log(line)
        lines.append(line)
    return lines, playing, age


def main():
    now = time.time()
    epoch_secs = get_epoch_secs()
    threshold_secs = 2 * epoch_secs
    headroom = get_headroom_near()
    threshold_near = headroom_alarm_threshold()
    alarm = headroom is not None and headroom < threshold_near

    out = []
    playing_count = 0
    max_age = 0
    for account, strategy, logname in fleet.FLEET:
        lines, playing, age = check_runner(
            account, strategy, logname, threshold_secs, now)
        out.extend(lines)
        if playing:
            playing_count += 1
            if age is not None:
                max_age = max(max_age, age)
    total = len(fleet.FLEET)
    if alarm:
        line = (f"ALARM-HEADROOM contract={CONTRACT} "
                f"headroom_near={fmt_near(headroom)} "
                f"threshold_near={fmt_threshold(threshold_near)}")
        append_watchdog_log(line)
        out.append(line)
    if playing_count < total:
        out.append(f"PLAYING-DEGRADED playing={playing_count}/{total}")
    elif not alarm:
        out.append(f"PLAYING-OK runners={playing_count}/{total} "
                   f"last_success_max_age_secs={max_age} "
                   f"headroom_near={fmt_near(headroom)}")
    for line in out:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
