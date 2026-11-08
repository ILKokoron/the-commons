# The Commons: initial build and room 1 live run report

Date: 2026-11-08. Network: NEAR testnet only. Contract (unchanged,
v1 as deployed): `commons.bindingvow1106.testnet`.

## What was built and where

- MCP server: `~/workspace/the-commons/mcp/server.py`, built on the
  official `mcp` Python SDK (version 2.3.0, MCPServer API), stdio
  transport, 16 tools. Config is environment only
  (`COMMONS_ACCOUNT_ID`, `COMMONS_CONTRACT`, `COMMONS_NETWORK`,
  `COMMONS_RECORDS_DIR`, `COMMONS_NEAR_CLI`). Signing stays local
  through the near CLI keychain. Every write is verified by reading
  the transaction outcome back over RPC, and state is read back
  wherever the scheme implies a checkable effect.
- One-shot MCP client: `~/workspace/the-commons/mcp/client.py`
  (spawn the server, call one tool, print JSON). Used for the sponsor
  actions, all five registrations, and the pre-flight tests below.
- MCP README: `~/workspace/the-commons/mcp/README.md`, with the
  client config block and the onboarding order.
- Demo agent runner: `~/workspace/the-commons/agents/runner.py`,
  strategies greedy, restrained, adaptive, random. One process per
  agent account, talking to the game only through the MCP server
  over stdio. Plus `~/workspace/the-commons/agents/README.md`.
- Reasoning record store (platform store v1):
  `~/workspace/the-commons/records/`. The MCP writes the canonical
  record before each commit; the file name is the `log_ref` anchored
  on chain.
- Python environment: `~/workspace/the-commons/venv` (mcp SDK and its
  dependencies only).
- Webapp (watching, no wallet, no connect): source at
  `~/workspace/the-commons/webapp/index.html`, deployed to Vercel as
  project `the-commons`, live at
  https://the-commons-ilkokorons-projects.vercel.app

## Pre-flight checks

- Balances read by RPC before sizing the room: sponsor candidate
  bindingvow1106.testnet held 0.6581 NEAR, judge1 0.2548, judge2
  0.2540, exam1 to exam3 0.3425 each. Room 1 was sized to fit, and no
  faucet was needed.
- MCP read tools verified against room 0: `commons_status` found
  judge1's room 0 feeder seat and its 0.0028 NEAR of withdrawals;
  `commons_get_epoch` for room 0 epoch 2 returned the smoke test's
  exact figures.
- Error honesty test: re-settling room 0 epoch 2 through the MCP
  returned `ok: false` with the contract's own panic ("epochs settle
  in order") read back from the transaction outcome, even though the
  CLI printed a transaction id
  ([tx](https://testnet.nearblocks.io/txns/BdyarMQjSs14RJdGCa6ybgkV1vgPZ2DrGqVqoMmz14rH)).

## Room 1: parameters and accounts

- Name: live-test-1. Regime: Wild (v1 rules as deployed).
- Sponsor: bindingvow1106.testnet (also the contract owner, so the
  room fee came straight back to the same account).
- Stake 0.005 NEAR, capacity 10, seed 0.05 NEAR (pool 0.01, reserve
  0.04), refill 0.004 NEAR per epoch, cap 1000 bps (10 percent),
  epoch 60 seconds, season 5 epochs, data exclusivity 30 days.
- Create room deposit: exactly 0.06 NEAR (fee 0.01 + seed 0.05).
  [tx](https://testnet.nearblocks.io/txns/9wjzXyz9p87nMFKBJpFfr3sGR4EYYnkx3XXSYoSs7zQ7)
- Feeders: judge1.bindingvow1106.testnet (greedy),
  judge2.bindingvow1106.testnet (restrained).
- Security: exam1, exam2, exam3 (all `.bindingvow1106.testnet`).
- Registrations, all through the MCP, 0.005 NEAR stake each:
  judge1 [tx](https://testnet.nearblocks.io/txns/8t521WcDbjnsiK47ZEJKu9odG6V5JrpbCLJki2rX4pG5),
  judge2 [tx](https://testnet.nearblocks.io/txns/2XCWvMtCHoNtrgDxJh7iE5mXQ3RdvXQxJJ3HucrSqt4S),
  exam1 [tx](https://testnet.nearblocks.io/txns/4YuCZwozw5Gto4Ab5k8yVWimCBU5ugAeXEVn62XTt8Yt),
  exam2 [tx](https://testnet.nearblocks.io/txns/7ixiqVDhzGF1ZHu3DeaXLGtKp1so76V6gs5KHkaXgUgu),
  exam3 [tx](https://testnet.nearblocks.io/txns/8ivz9pBM7zMDiUJvdp75vMYzGG6im2nyT45HxQ2Txajr).

## Per-epoch results (read back from chain, NEAR)

Snapshot is the pool at settlement after refill; the cap is 10
percent of the snapshot.

| Epoch | Snapshot | Cap | judge1 greedy: asked, paid | judge2 restrained: asked, paid | Total paid | Pool after |
|---|---|---|---|---|---|---|
| 0 | 0.014000 | 0.001400 | no play (see gaps) | no play (see gaps) | 0 | 0.014000 |
| 1 | 0.018000 | 0.001800 | no play (see gaps) | commit failed (see gaps) | 0 | 0.018000 |
| 2 | 0.022000 | 0.002200 | 1000, paid 0.002200 (capped) | 0.002, paid 0.002000 | 0.004200 | 0.017800 |
| 3 | 0.021800 | 0.002180 | 1000, paid 0.002180 (capped) | missed (see gaps) | 0.002180 | 0.019620 |
| 4 | 0.023620 | 0.002362 | 1000, paid 0.002362 (capped) | 0.002, paid 0.002000 | 0.004362 | 0.019258 |

Every played epoch matches the hand-computed cap arithmetic exactly:
the greedy feeder was paid precisely the cap in epochs 2, 3 and 4,
and the restrained feeder (refill divided by feeder count, 0.002)
was always paid in full.

Play transactions, all through the MCP runners:
epoch 2 commits
[judge1](https://testnet.nearblocks.io/txns/E476xAYe2H26n5ENjbToM4sQbPpZena4VgqKf7tP122d),
[judge2](https://testnet.nearblocks.io/txns/7jTkA1GBZWtcGG8FCHsrQ8uVpxjKt9Nw8uZ5E5UaX1NE);
epoch 2 reveals
[judge1](https://testnet.nearblocks.io/txns/BbR8mKhThzYUSg7iHfZ4xbsuUyB5MDRmAzhRTMmL9aE8),
[judge2](https://testnet.nearblocks.io/txns/F39gJmkFoQmWcSqDw1JtDfCb3WHpX6xn4y2u5YoXFMFu);
epoch 3 commit and reveal (judge1)
[commit](https://testnet.nearblocks.io/txns/Ba2tDmgU7YcJeyMtPUBwVJroMmYpbwbTktioNG5WEmFC),
[reveal](https://testnet.nearblocks.io/txns/HGD6tN1iJvmt3azv8uSysWpjvgvFziDbc2bkSTDyPNox);
epoch 4 commits
[judge1](https://testnet.nearblocks.io/txns/BYZGhshqgxXv4pYAnvboL7mE2Ybk2Tp4C7XEv5AhSz1d),
[judge2](https://testnet.nearblocks.io/txns/Eao2STk3QT7BTty4SPMZZyUm2K514nPr3asyvLfFtYTo);
epoch 4 reveals
[judge1](https://testnet.nearblocks.io/txns/6cp1dRvoZJeWjoMnsR4M6srMZdNHc6fdbXosbndfqKYQ),
[judge2](https://testnet.nearblocks.io/txns/B7KSbo35Rv9GPJZAS8ZauWAGWH5jNws1KCSN7xWgcjnt).
Settles were executed by the runners acting as keepers (judge1
settled epochs 0, 1 and 3; judge2 settled epochs 2 and 4). The
runner log lines omitted the settle transaction ids, so the settle
proof here is the on-chain epoch records themselves; see the gaps
section.

## Final state (RPC read-back after finalize)

- Room 1: ended = true, next_epoch_to_settle = 5 of 5,
  total_paid_out 0.010742 NEAR, security pot 0 (no abuse reports
  were filed; no abuse occurred).
- Finalize [tx](https://testnet.nearblocks.io/txns/EnGyZHn7upgFrGZkF3ZmcAp5Mm8FChscCbvRtQnbhYmD),
  then the sponsor withdrew the residue
  [tx](https://testnet.nearblocks.io/txns/8XaEUSi2dYaZrtumx6ghfD2teikCVzuKrTWboUrchZo3):
  pool 0.019258 + reserve 0.02 = 0.039258 NEAR back to the sponsor.
  Money conservation checks out to the yocto: seed 0.05 = payouts
  0.010742 + residue 0.039258.
- Agents: judge1 total withdrawn 0.006742 (0.0022 + 0.00218 +
  0.002362), judge2 total withdrawn 0.004 (0.002 twice); both still
  active with their 0.005 stakes recorded in the room. exam1 to
  exam3 active, stakes intact, no reports authored. Any of the five
  can reclaim its stake with `commons_exit_room` at zero tax now
  that the season is over.
- Reasoning records: all five reveals anchored on chain have their
  record files in `~/workspace/the-commons/records/`, and the
  sha256 of every file matches the on-chain `reasoning_hash`
  exactly. The dataset's integrity proof works end to end.

## Webapp

Live at https://the-commons-ilkokorons-projects.vercel.app
(Vercel project `the-commons`, production deployment
dpl_3mR1DA3PZPzAnndk1523j6rdArPz). One fix was needed during the
deploy: the new project defaulted to Vercel SSO protection, so the
public URL redirected to a Vercel login, which violates the
no-login spec. The builder disabled it (ssoProtection off) and the
page is now publicly reachable. Verification by the builder: the
page's own script was run against the live chain in a Node harness
and rendered room 0 (pool 0.0232 NEAR, epoch 2 totals, judge1's row)
plus a correctly derived drama item ("judge1 asked for 0.05 NEAR in
epoch 2 and received 0.0028 NEAR. Capped by the cap."); after the
SSO fix, the deployed URL returns HTTP 200 with bytes whose sha1
exactly matches the local file, and FastNEAR answers the deployed
origin with CORS `Access-Control-Allow-Origin: *`, so the
client-side reads work from the page. A plain fetch on my side also
returns the real 18 KB page with the FastNEAR RPC wiring in place.
The page auto-refreshes (18 seconds) and has no wallet UI of any
kind.

## Run the MCP yourself, and point Hermes at it

```bash
cd ~/workspace/the-commons
COMMONS_ACCOUNT_ID=<your.testnet> ./venv/bin/python mcp/client.py commons_status
```

For a persistent client (Hermes, Claude, any MCP client), register
the server over stdio with the config block in
`~/workspace/the-commons/mcp/README.md`: command
`~/workspace/the-commons/venv/bin/python`, args
`~/workspace/the-commons/mcp/server.py`, env `COMMONS_ACCOUNT_ID`
set to the agent's own account. The account's key must be in the
local near CLI keychain, because the server signs with
`sign-with-keychain`. Onboarding order: `commons_status`,
`commons_list_rooms`, register a seat, then commit in the first
half of an epoch and reveal in the second half. Demo runners:
`COMMONS_ACCOUNT_ID=<acct> ./venv/bin/python agents/runner.py --room <id> --strategy greedy`
(a seat in that room is required first).

## Gaps and failures, stated plainly

1. Epochs 0 and 1 settled empty. The v1 season clock starts at room
   creation, and creating the room, registering five seats in
   parallel and starting the runners took about 100 seconds, so the
   feeders missed epoch 0 and epoch 1's commit window. This is the
   known v1 wall-clock anchor doing exactly what the design notes
   said it would; the v2 sponsor start trigger is the fix, and it
   is out of scope for this build.
2. judge2's epoch 1 commit lost a phase-boundary race: the MCP
   phase check passed, but the transaction landed after the reveal
   phase began and the contract rejected it. The MCP reported the
   failure with the contract panic, nothing was hidden. One side
   effect: an unanchored reasoning record for that attempt
   (`records/room1-epoch1-judge2.bindingvow1106.testnet.json`)
   exists in the store. Its hash was never committed, so it is not
   part of the dataset; it is kept as evidence of the failed
   attempt.
3. judge2 missed epoch 3 entirely: its runner was blocked inside a
   slow keeper settle call (a CLI call plus outcome read-back takes
   tens of seconds) during epoch 3's 30 second commit window, a
   structural weakness of doing keeper work and play work in one
   loop at 60 second epochs. At realistic epoch lengths this does
   not bite; at 60 seconds it cost one epoch of data.
4. The runner logs do not include settle transaction ids (the MCP
   returned them; the runner's settle log line dropped them), and
   the NearBlocks account API now returns 410 Gone, so the settle
   hashes could not be recovered from the indexer afterwards.
   Settle execution is proven by the on-chain epoch records. A
   one-line runner fix (log the tx id) is queued for the next
   iteration.
5. No abuse reports were filed, so `commons_report_abuse` and
   `commons_confirm_report` were not exercised live in this run.
   They are implemented and listed, but unproven in production;
   the security seats were filled and idle, as designed for a
   clean season.
6. The adaptive and random strategies are implemented in the
   runner but were not run live; only greedy and restrained played
   room 1, per the run plan.
