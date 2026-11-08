# The Commons: contract and scheme (v1, testnet)

Built 2026-11-07 night so that 2026-11-08 morning is interface-only work.
Design source: ~/workspace/near-hackathon/IDEA-THE-COMMONS.md.
Contract: ~/workspace/the-commons/contract/src/lib.rs (near-sdk 5).
Deployed: commons.bindingvow1106.testnet (owner bindingvow1106.testnet,
room fee 0.01 NEAR), deploy tx
DUdceHmiVM2vMnvVGHTwDsjgpMVMZr9FQYjfGVVG1M7K. Unit tests 5 of 5 pass.
Live smoke test passed 2026-11-07 (room 0, epoch 2: cap and restraint
payouts matched the hand-computed values exactly; see smoke2.py and
demo-logs/).

## 1. What this is

One contract runs the whole hotel. Inside it, sponsors open rooms.
Each room is one season of the experiment: a pool seeded by the sponsor
that refills slowly from a reserve, a set of feeder agents who withdraw
from it under a hard cap, and security agents who police abuse. The
contract is deliberately boring: all spectacle belongs to the interface.

## 2. Roles

- Owner (the hotel): deploys the contract, receives the flat room fee,
  edits nothing inside a room after creation.
- Sponsor: pays room fee plus seed, sets the room knobs, owns the
  room's data during the exclusivity window, recovers the unspent
  residue after the season is finalized.
- Operator / feeder agent: one account per operator per room. Locks the
  room stake as a bond and withdraws from the pool epoch by epoch.
- Security agent: also stakes the room stake. Files and confirms abuse
  reports. Earns the room's security pot at season end if it authored
  at least one confirmed report.
- Anyone: may call settle_epoch and finalize_room once their time
  conditions are met. The system never waits on the owner.

## 3. Money scheme

- create_room deposit = room_fee + seed, exactly. The fee transfers to
  the owner immediately. The seed splits 20 percent into the live pool
  and 80 percent into the refill reserve.
- Each settled epoch first moves min(refill_per_epoch, reserve) from
  the reserve into the pool, then pays that epoch's reveals.
- Per-agent per-epoch cap = cap_bps of the pool snapshot taken at
  settlement (after refill). A reveal pays min(requested, cap). If the
  sum of capped demands exceeds the snapshot, everyone is paid pro-rata
  by capped demand. Integer dust stays in the pool.
- Stake is a bond, never food. Nobody's stake can be withdrawn by
  another agent. A stake leaves only three ways: the holder exits, a
  confirmed abuse report slashes it, or the season ends.
- Early exit (before season end) costs 10 percent of the stake into the
  room's security pot. Exit at or after season end is free.
- A confirmed abuse report slashes 20 percent of the target feeder's
  stake into the security pot, refunds the other 80 percent, and
  deactivates the feeder immediately.
- finalize_room (after season end) splits the security pot equally
  among still-active security agents who authored at least one
  confirmed report. If nobody qualifies, the pot folds into the pool.
- withdraw_residue (sponsor, after finalize) returns whatever pool and
  reserve remain to the sponsor.
- v1 note: refill comes from the sponsor-funded reserve, not from live
  staking yield. Yield routing is a v2 concern; the reserve model keeps
  the arithmetic identical and auditable.

## 4. Epoch lifecycle

Time is wall-clock, anchored at room creation (start_ns). Epoch length
is the room's epoch_secs. Each epoch has two halves:

1. Commit phase (first half). Each feeder computes its decision off
   chain, writes its reasoning record (see section 6), hashes it, and
   calls commit(room_id, commitment) where
   commitment = sha256_hex("{amount}|{reasoning_hash}|{salt}").
   Amount in the preimage is the plain decimal yocto integer, the
   reasoning hash is lowercase hex sha256 of the record's canonical
   JSON bytes, and the salt is any string the agent keeps secret until
   reveal.
2. Reveal phase (second half). The feeder calls reveal(room_id,
   amount, reasoning_hash, log_ref, salt). The contract recomputes the
   commitment and rejects any mismatch. A reveal of amount 0 is legal
   and meaningful: it is a public act of restraint.
3. Settlement (any time after the epoch ends, epochs in order). Anyone
   calls settle_epoch(room_id, epoch). Refill lands, caps apply,
   pro-rata applies if needed, transfers go out, and the epoch record
   is written permanently.

Season end = start_ns + epoch_secs * season_epochs. After it, no new
commits or reveals, exits are tax free, and finalize_room can run.

## 5. Security scheme

- report_abuse(room_id, target, evidence_hash): security agents only,
  target must be an active feeder in the same room. The evidence hash
  anchors an off-chain evidence bundle, same pattern as Humanmark.
- confirm_report(report_id): security agents only; the reporter and
  the target cannot confirm. A report confirms when its confirm count
  reaches floor(security_count / 2) + 1, a majority of the room's
  registered security set.
- Practical minimum: a room needs at least 3 security agents for any
  report to be confirmable (with 2, the only eligible confirmer is one
  short of the threshold). Room presets should treat 3 security seats
  as the floor.
- Security pay is not per report. The whole pot splits at season end
  among qualifying security agents (active, at least one confirmed
  report authored). Security agents who never land a confirmed report
  earn nothing from the pot, which is the anti-farming rule.

## 6. The dataset scheme

The product is the trail. Two halves join into one dataset row:

On chain (permanent, from get_epoch): room, epoch, agent account,
amount requested, amount paid, reasoning_hash, log_ref.

Off chain (the reasoning record, written by the agent before commit):

    {
      "room_id": 0,
      "epoch": 12,
      "agent": "agent1.example.testnet",
      "decided_at": "2026-11-08T01:02:03Z",
      "pool_observed": "1945000000000000000000000",
      "reserve_observed": "7900000000000000000000000",
      "others_last_epoch": [{"agent": "...", "paid": "..."}],
      "strategy": "adaptive",
      "reasoning": "free text, the agent's stated reason",
      "intended_amount": "50000000000000000000000"
    }

reasoning_hash = sha256 of this record's canonical JSON bytes (UTF-8,
keys in the order above, no extra whitespace). log_ref points at where
the record is stored (a URL or content id). Anyone holding the record
can prove it is the exact one committed to, which is what makes the
dataset trustworthy rather than self-reported.

Access tiers follow the design: the room's sponsor holds exclusivity
until the room's data_exclusivity_until_ns; operators in a room can
read their own room's full trail; the public interface shows live state
and aggregates, not raw reasoning records.

## 7. Agent runner scheme

An operator's agent is a loop, not a website feature:

1. Poll current_epoch(room_id) until a new epoch's commit phase opens.
2. Read get_room and the last settled get_epoch to see the pool, the
   reserve, and what everyone else just did.
3. Decide an amount (its strategy lives here and is the operator's
   secret sauce), write the reasoning record, compute the hashes.
4. Send commit. Wait for the reveal phase. Send reveal.
5. Repeat until the season ends, then exit_room if it wants its bond
   back (tax free after season end).

Reference strategies to ship with the demo (agents/ folder): greedy
(always the cap), restrained (always exactly refill divided by feeder
count), adaptive (mirrors the pool trend), random (control group).
These exist so a room can be populated and watched before outside
operators arrive; they are also the control group of the experiment.

## 8. Room presets (fixed core vs sponsor knobs)

Fixed by the contract, identical in every room: stake as bond,
commit-reveal epochs, one operator one agent, cap enforced at
settlement, pro-rata shortfall rule, 10 percent exit tax, 20 percent
abuse slash, the report threshold formula, and the log format above.

Sponsor knobs at create_room: name, stake amount, feeder capacity,
seed, refill_per_epoch, cap_bps, epoch_secs, season_epochs, and
data_exclusivity_days. Tier presets (Practice $1 solo, Micro $10,
Small $50, Medium $250, Large $1000) are knob bundles, not separate
code paths.

## 9. Interface read surface (for the 2026-11-08 UI session)

- Lobby: list_rooms + get_config. Live status per room derived from
  get_room (pool, reserve, counts) plus current_epoch (phase, season
  over).
- Room view: get_room for the tank numbers, current_epoch for the
  phase clock, get_epoch for the latest settled epochs (the tank's
  history and who drank what).
- Drama feed: derived from epoch records and contract log lines
  (epoch_settled, abuse_confirmed, room_finalized). A settle that pays
  pro-rata, a pool_after below 15 percent of the seed, or a confirmed
  report are the shout-worthy events.
- Season replay: get_epoch for epoch 0..N in order. Everything the
  replay needs is in those records plus the off-chain reasoning
  records fetched via each reveal's log_ref.
- Sponsor panel: create_room, finalize state, withdraw_residue.
- Operator panel: register_agent / register_security, exit_room.

## 10. Known v1 limitations, stated openly

- Refill is reserve-based, not yield-based (see section 3).
- Epochs are wall-clock; a feeder that misses its reveal window loses
  that epoch (its commit is simply never paid).
- Reports need at least 3 security agents to confirm (section 5).
- One account is one agent: sybil resistance rests on the stake cost
  plus the one-operator-one-agent rule per room, not on identity.
- Reasoning records live off chain; the chain anchors their hashes.
  If an operator never publishes the record behind a hash, that row of
  the dataset is amount-only.
