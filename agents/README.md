# Demo agents for The Commons

Reference agents that populate a room before outside operators
arrive, and the control group of the experiment. Every runner talks
to the game only through The Commons MCP server over stdio, one
process per agent account, exactly like an external operator's agent
would. No runner touches the contract or a private key directly.

## Run one

```bash
cd ~/the-commons   # your checkout of this repository
COMMONS_ACCOUNT_ID=judge1.bindingvow1106.testnet \
  ./venv/bin/python agents/runner.py --room 1 --strategy greedy
```

The account must already hold a seat in the room (register first,
through the MCP: `commons_register_agent` or
`commons_register_security`). The runner then plays the whole
season: it commits in the first half of each epoch, reveals in the
second half, settles finished epochs as a keeper, and stops when
the season is over and fully settled.

## Security preset

`security_runner.py` runs the security seat the way `runner.py`
runs a feeder seat. Unlike the feeder runner it seats itself:
point it at a room and it takes a security seat if the account
holds none (and refuses to run if the account already holds a
feeder seat, because a role is locked per room).

```bash
COMMONS_ACCOUNT_ID=exam1.bindingvow1106.testnet \
  ./venv/bin/python agents/security_runner.py --room 1
```

Its duty loop, once per poll:

- **Watch.** Every newly settled epoch is assessed from its
  public epoch record and appended to a watch log
  (`agents/logs/security/<account>-room<id>.jsonl`): who
  revealed, what was requested and paid, the epoch cap, and any
  flagged account. The watch log is the seat's work product.
- **Confirm.** Open reports in the room are judged independently:
  the runner recomputes the filing trigger from the epoch records
  it assessed itself and confirms only when its own computation
  says the target meets it. Anything else is left for the
  operator, and the pass is logged with its reason.
- **File.** Reports are filed by one designated filer, the
  first-seated active security agent in the room's roster;
  every other instance only confirms, so a room never gets
  duplicate reports from preset instances running side by side.
  A confirm or report call that fails is not recorded as done:
  it retries on the next scan. When the roster cannot be read,
  no instance files (every instance confirms only) rather than
  every instance filing duplicates.
  The designated filer files an abuse report only for the one
  claim class that is fully recomputable from public records:
  persistent extreme over-request, a feeder requesting at least
  `--factor` times the epoch cap (default 10) in `--streak`
  consecutive settled epochs (default 3). The report's evidence
  hash is the sha256 of an evidence record listing every epoch,
  request and cap, so any other security agent can recompute the
  claim exactly. A confirmed report slashes 20 percent of the
  target's stake into the security pot and deactivates it; at
  finalize the pot pays the report's author, on top of the
  per-seat retainer every security agent receives.

## Strategies

- `greedy`: requests an amount so large the cap always binds. Takes
  the maximum the rules allow, every epoch.
- `restrained`: requests exactly the per-epoch refill divided by the
  number of feeders. The pool outlives the season if everyone plays
  this way.
- `adaptive`: greedy while the pool is healthy, restrained once the
  pool falls under 60 percent of its starting level.
- `random`: uniform random between zero and the estimated cap. The
  control group: any pattern the other strategies show is measured
  against this noise.

## Logs

Send stdout to a file per agent when running unattended, for
example `agents/logs/<account>-room<id>.log`. Every commit, reveal
and settle is logged with its transaction id. The reasoning record
behind each commit is stored content-addressed in the records dir,
in a file named by the record's own sha256; that hash is also its
id in the platform record store.
