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
