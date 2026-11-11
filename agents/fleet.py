#!/usr/bin/env python3
"""Fleet launcher and status for The Commons demo runners (room 2).

Single source of truth for which demo accounts run, with which
strategy, and into which log file. Used by the watchdog and by hand:

  python3 fleet.py start          start every runner not already up
  python3 fleet.py start ACCOUNT  start one runner
  python3 fleet.py status         show pid and liveness per runner

Each runner is started detached in its own session (start_new_session)
so it survives the shell or agent session that launched it, with
stdout and stderr appended to its log file. Its pid is recorded under
agents/state/pids/<account>.pid. Liveness is checked against
/proc/<pid>/cmdline so a recycled pid is never mistaken for a runner.
"""

import fcntl
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VENV_PY = os.path.join(ROOT, "venv", "bin", "python")
RUNNER = os.path.join(HERE, "runner.py")
STATE_DIR = os.path.join(HERE, "state")
PID_DIR = os.path.join(STATE_DIR, "pids")
ROOM = int(os.environ.get("COMMONS_ROOM_ID", "2"))

# (account, strategy, log file name inside agents/logs/)
FLEET = [
    ("judge1.bindingvow1106.testnet", "greedy",
     "judge1-room2.log"),
    ("judge2.bindingvow1106.testnet", "restrained",
     "judge2.bindingvow1106.testnet-room2.log"),
    ("judge3.bindingvow1106.testnet", "adaptive",
     "judge3.bindingvow1106.testnet-room2.log"),
    ("exam2.bindingvow1106.testnet", "random",
     "exam2.bindingvow1106.testnet-room2.log"),
    ("exam3.bindingvow1106.testnet", "greedy",
     "exam3.bindingvow1106.testnet-room2.log"),
    ("exam5.bindingvow1106.testnet", "restrained",
     "exam5.bindingvow1106.testnet-room2.log"),
    ("bindingvow1106.testnet", "greedy",
     "bindingvow1106.testnet-room2.log"),
    ("humanmarkfund1107.testnet", "adaptive",
     "humanmarkfund1107.testnet-room2.log"),
    ("commons.bindingvow1106.testnet", "random",
     "commons.bindingvow1106.testnet-room2.log"),
]


def log_path(logname):
    return os.path.join(HERE, "logs", logname)


def pid_path(account):
    return os.path.join(PID_DIR, f"{account}.pid")


def read_pid(account):
    try:
        return int(open(pid_path(account)).read().strip())
    except Exception:
        return None


def pid_alive(pid, account):
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().decode(errors="replace")
    except Exception:
        return False
    return "runner.py" in cmd and account in cmd


def running_pid(account):
    pid = read_pid(account)
    return pid if pid_alive(pid, account) else None


def start_runner(account, strategy, logname):
    """Start one runner unless it is already running.

    Returns (pid, started_bool)."""
    existing = running_pid(account)
    if existing:
        return existing, False
    os.makedirs(PID_DIR, exist_ok=True)
    env = dict(os.environ)
    env["COMMONS_ACCOUNT_ID"] = account
    lf = open(log_path(logname), "a")
    proc = subprocess.Popen(
        [VENV_PY, RUNNER, "--room", str(ROOM), "--strategy", strategy,
         "--account", account],
        stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        env=env, cwd=HERE, start_new_session=True)
    lf.close()
    with open(pid_path(account), "w") as f:
        f.write(str(proc.pid))
    return proc.pid, True


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("start", "status"):
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    only = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "start":
        # Serialize concurrent starts: two overlapping invocations
        # could otherwise both pass the already-running check and
        # spawn two runners for one account on one state file.
        os.makedirs(PID_DIR, exist_ok=True)
        _lock_fd = os.open(os.path.join(PID_DIR, "start.lock"),
                           os.O_CREAT | os.O_RDWR)
        fcntl.flock(_lock_fd, fcntl.LOCK_EX)
    for account, strategy, logname in FLEET:
        if only and account != only:
            continue
        if cmd == "start":
            pid, started = start_runner(account, strategy, logname)
            print(f"{account}: pid={pid} "
                  f"{'started' if started else 'already running'}")
        else:
            pid = running_pid(account)
            print(f"{account}: {'alive pid=' + str(pid) if pid else 'DOWN'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
