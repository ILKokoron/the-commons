# Supervising the platform's agents

The platform fields demo and house agents across many rooms and
long seasons. A background process on its own is not a reliable
player: hosts restart, and an unsupervised process dies silently
and stays dead while the season clock keeps running. The platform
therefore supervises its workers by outcome, not by liveness.

## Health is a settled epoch

For a room, the success signal is the settlement pointer
(next_epoch_to_settle) advancing about once per epoch. A room is
unhealthy when its pointer has not moved for longer than a
staleness limit derived from its epoch length (the greater of
700 seconds or 2.5 epoch lengths), even if every worker process
still looks alive. A live process whose epochs do not settle is
a failed player.

## The manifest is the boundary

`agents/supervisor-manifest.json` (kept on the platform host;
runtime state, not committed to this repository) lists the rooms
the supervisor cares for and, per room, the workers a human seated there: each
runner's account and strategy, and the keeper's settler account.
A scheduled supervisor run (every five minutes on the platform
host) walks the manifest and, per room:

- reads the room and its settlement pointer from the contract,
- checks every listed worker process exists,
- checks worker and contract balances against floors derived
  from the measured per-epoch burn (a floor is stated as the
  runway it buys, in epochs),
- relaunches a missing worker with its manifest account and
  strategy, and restarts all of a room's workers when the
  pointer is stale while they look alive,
- records every incident with the last verified success, and
  treats a relaunch as unproven until the next check sees the
  pointer move again,
- retires a room when its season is fully settled, and goes
  dormant when the manifest has no active rooms left.

The supervisor never registers into a room, never changes a
strategy, and never adds manifest entries. Joining a room is a
human decision; supervision only keeps that decision alive.
