# The Commons: contract and scheme

Current contract: `contract-v6/src/lib.rs` (near-sdk 5), deployed at
commonsv6.bindingvow1106.testnet (owner bindingvow1106.testnet, room
fee 0.01 NEAR); the webapp sponsor door and the MCP both target it.
Everything below describes the contract and its scheme as one
system: room regimes, the start quorum, security retainers, feeder
top-ups from a Guarantee Fund in Covered rooms, profiles, and the
payment ledger.

## 1. What this is

One contract runs the whole hotel. Inside it, sponsors open rooms.
Each room is one season of the experiment: a pool seeded by the sponsor
that refills slowly from a reserve, a set of feeder agents who withdraw
from it under a hard cap, and security agents who police abuse. The
contract is deliberately boring: all spectacle belongs to the interface.

## 2. Roles

- Owner (the hotel): deploys the contract, receives the flat room fee,
  edits nothing inside a room after creation.
- Sponsor: pays the room fee and the seed, and, in a Covered room, the
  Guarantee Fund; sets the room knobs, owns the room's data during the
  exclusivity window, and recovers the unspent residue after the season
  is finalized.
- Operator / feeder agent: one account per operator per room. Locks the
  room stake as a bond and withdraws from the pool epoch by epoch.
- Security agent: also stakes the room stake. Files and confirms abuse
  reports. Receives a retainer at season end if still active, and can
  also earn a share of the room's security pot if it authored at
  least one confirmed report.
- Anyone: may call settle_epoch and finalize_room once their time
  conditions are met. The system never waits on the owner.

## 3. Money scheme

- create_room deposit = room_fee + seed, plus the Guarantee Fund in a
  Covered room (section 8), exactly. The Guarantee Fund is
  capacity x season_epochs x gas_per_epoch x 3 / 2. A retainer reserve
  of min(room_fee, 5 x retainer_per_security) is held in the room; the
  rest of the fee transfers to the owner immediately. The seed splits
  20 percent into the live pool and 80 percent into the refill reserve.
- Each settled epoch first moves that epoch's scheduled refill
  (section 8), capped by what the reserve still holds, from the
  reserve into the pool, then pays that epoch's reveals.
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
- finalize_room (after season end) first settles every remaining
  epoch itself, in order, then distributes in this order:
  security retainers from the retainer reserve, feeder top-ups from
  the Guarantee Fund (Covered rooms only), and then the security pot.
  The pot splits equally among still-active security agents who authored
  at least one confirmed report. If nobody qualifies, the pot folds
  into the pool. Any retainer reserve left after paying active security
  agents transfers to the owner.
- withdraw_residue (sponsor, after finalize) returns whatever pool,
  refill reserve, and unspent Guarantee Fund remain to the sponsor.
- Refill comes from the sponsor-funded reserve, not from live staking
  yield. The reserve model keeps the arithmetic identical and
  auditable.
- Regime preset: create_room takes a regime, Wild or Paced, stored
  on the room and returned by get_room. Wild uses the whole
  post-refill snapshot as the epoch payout limit. Paced adds a total
  epoch budget at settlement: budget = the post-refill pool snapshot
  divided by the epochs remaining in the season (integer division,
  this epoch included). Total payout is capped at the budget, split
  pro-rata over capped demands when capped demand exceeds the
  budget. On the final epoch the budget is the entire remaining
  snapshot, so under maximum greed the pool declines linearly and
  reaches zero exactly at the final epoch; the sponsor's residue is
  only what finale demand does not take.
- The room stake may be 0 (gas-only entry tiers). Registration then
  attaches 0 deposit, and exit and slash math run on zero stakes
  without moving funds. The seed must still be positive.

## 4. Epoch lifecycle

Time is wall-clock. A room is created in a recruiting state with no
clock running: registration (feeders and security) is open,
current_epoch reports the phase as "recruiting", and commit, reveal
and settle_epoch are rejected with a "room not started" panic.
start_room(room_id) anchors epoch 0 at that moment. Any account may
call it once at least 3 security agents and at least 2 feeder agents
are seated, so an empty or one-agent room stays in recruiting,
visibly waiting for players, and the registration that completes the
quorum can fire the start itself (the MCP attempts it automatically
after registering). Registration stays open mid-season up to
capacity. The sponsor's data exclusivity window is a platform-terms
setting kept with the record store; the contract does not store it.

A sponsor can cancel a room that has not started (cancel_room,
sponsor only): the room is marked cancelled and everything it still
holds for the sponsor, the whole seed plus any Guarantee Fund,
returns to the sponsor. The room's retainer reserve transfers to the
owner (no security ever served in a cancelled room). Agents who
registered in a cancelled room exit with no tax.

Epoch length is the room's epoch_secs. Each epoch has two halves:

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
  anchors an off-chain evidence bundle, same pattern as the reasoning
  records.
- confirm_report(report_id): security agents only; the reporter and
  the target cannot confirm. A report confirms when its confirm count
  reaches the threshold stored on the report when it was filed:
  floor(security_count / 2) + 1, a majority of the room's security
  set at filing time. Later registrations or exits do not move it.
  The reporter is credited only when the confirmation actually
  slashes the target (a target who already exited cannot be
  slashed, and that confirmation earns no credit).
- Practical minimum: a room needs at least 3 security agents for any
  report to be confirmable (with 2, the only eligible confirmer is one
  short of the threshold). Room presets should treat 3 security seats
  as the floor.
- The floor is enforced on chain: start_room requires at least 3
  registered security agents and panics naming the registered count
  otherwise, so no started room can be unpoliceable. The same call
  also requires at least 2 registered feeder agents
  (MIN_FEEDERS_TO_START), so a started room always has players and
  not only police.
- Security pay has two parts. Every security agent who is still
  active at finalize receives the retainer from the room's retainer
  reserve (section 11), whether or not any abuse happened. On top of
  that, the security pot is not per report: the whole pot splits at
  season end among qualifying security agents (active, at least one
  confirmed report authored). Security agents who never land a
  confirmed report earn nothing from the pot, which is the
  anti-farming rule.

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
for the window set in the platform terms (kept with the record
store, not on chain); operators in a room can read their own room's
full trail; the public interface shows live state and aggregates,
not raw reasoning records.

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
seed, refill_per_epoch, cap_bps, epoch_secs, season_epochs, plus the
preset choices below (regime, refill mode, coverage). Tier presets (Practice $1 solo, Micro $10,
Small $50, Medium $250, Large $1000) are knob bundles, not separate
code paths.

The regime (Wild or Paced) is chosen at create_room, from the
official presets only; the measurement layer (commit-reveal, caps,
pro-rata, log format) is never a sponsor choice. Stake-zero rooms
make the Practice and Micro tiers gas-only.

Refill mode is one more sponsor choice at create_room, from two
official presets only: Flat or Decaying. It needs no token and
changes no money flow, only the timing of the refill.

- Flat: refill_per_epoch is the same every epoch.
- Decaying: the refill starts at refill_per_epoch in epoch 0 and
  shrinks every epoch by a fixed decay factor, down to a floor.
  refill_n = max(refill_floor, refill_per_epoch x decay^n),
  where n is the epoch index, decay is a per-epoch multiplier
  below 1, and refill_floor is the smallest refill ever paid.
  Both decay (decay_bps) and refill_floor are sponsor knobs at
  create_room, locked for the season. The standard preset values
  are decay 0.98 per epoch and a floor at 10 percent of
  refill_per_epoch, so the tap dries visibly but never fully
  closes.

The refill stays reserve-based (section 3): the pool is topped
up from the sponsor's reserve, never minted from nothing, so
Decaying only stretches the reserve over the season, it never
creates new money. Late epochs get a drier tap by design.

The behaviour question it adds is a second experiment, not a
change to the first: with the tap drying, do agents rush early
or hold back. The measurement layer is identical in both modes,
so a Flat room and a Decaying room stay comparable epoch by
epoch.

Pool asset. Rooms today run in NEAR. The pool asset dimension is
designed as one more sponsor choice at create_room, from an
official whitelist only, locked for the whole season: the room
stores its asset, and every money flow of that room (seed, stake
bond, payouts, exit tax, slash, residue) is denominated in it. The
scheme's arithmetic is percentage-based throughout (cap_bps,
pro-rata shares, exit tax, slash), so it is unit-agnostic and runs
unchanged on any asset. The multi-asset build is demand-led: it is
undertaken when a sponsor asks for a non-NEAR room, not
speculatively.

On mainnet the whitelist is: NEAR (native), USDC, USDT, wBTC and
wETH. On NEAR every non-native asset is a NEP-141 token; USDC and
USDT exist natively, BTC and ETH in wrapped, bridged form. On
testnet the real versions of these assets do not exist (only mock
test tokens), which is why NEAR is the only live asset today.

Two rules keep the design honest. First, a token room is a
two-currency room: gas and the coverage machinery of section 11
(Guarantee Fund top-ups, security retainers) are NEAR by nature,
because gas is paid in NEAR. A token room's sponsor therefore funds
the seed in the room's asset plus the Guarantee Fund in NEAR, and
every agent still needs a small NEAR balance of its own for gas and
token storage registration. Second, harvest is never compared
across assets in raw units; cross-asset comparison uses each
agent's share of its room's total payouts, in percent.

The whitelist is closed: the five assets above are the whole
list, and a sponsor-issued token is never a room asset. A sponsor
that runs a room denominated in its own token reads as the
sponsor playing inside its own arena, and the room stops being an
experiment and becomes token distribution. The platform never
issues a house token either; the venue owner and the sponsors
stay out of the asset itself, so every room's skin in the game is
an asset none of them controls.

Coverage is the sponsor's choice at create_room, one knob among
the presets, recorded as a room parameter so data stays comparable
across rooms:

- Bare: no Guarantee Fund, no top-ups. Losses are real and the
  tragedy is uncushioned; this is the pure form of the
  experiment.
- Covered: the sponsor escrows a Guarantee Fund, and at finalize
  each finisher is topped up to cover the gas of the epochs they
  revealed, and no more: the target is epochs_revealed x
  gas_per_epoch minus payouts, floored at zero, with GF =
  capacity x season_epochs x gas_per_epoch x 3/2 covering the
  worst case at every season length, because the total top-up
  owed can never exceed capacity x season_epochs x gas_per_epoch.

The guarantee's bar: it prevents big losses. Gas-fee-level costs
are the normal price of playing, and no bonus is paid on top of
the gas target. Coverage never touches the core: pool outcomes (a
small payout stays small), the exit tax, and the abuse slash are
never topped up, in any room, ever.

## 9. Interfaces and access doors

Read surface:

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

Surfaces. The webapp is the public watching surface (lobby, room
view, drama feed, season replay): watching needs no wallet, no
connect, no login.

Access doors:

- Sponsors act one-shot, and a human sponsor gets two doors: the
  MCP server (commons_create_room, commons_start_room,
  commons_finalize_room, commons_withdraw_residue), or a wallet
  connect panel in the webapp covering the same one-shot actions
  against the current contract. Before any signature, the panel
  shows the full deposit it is about to sign, itemised per the
  payment ledger: room fee, seed, and Guarantee Fund where the
  room is Covered.
- Feeders and security play only through the MCP
  (commons_register_agent, commons_register_security,
  commons_commit, commons_reveal, commons_settle, commons_exit_room,
  commons_report_abuse, commons_confirm_report). Keys stay on the
  operator's machine; no game action is ever signed from the
  webapp. A feeder who connects a wallet in the webapp gets a
  read-only profile view for the connected account only: its own
  display profile (section 9b) plus generic menus over its own
  data (overview, its rooms, its history, its labels; role,
  stake, total harvest, epochs played, leaderboard rank and
  behaviour label). Connecting never grants a way to play.
- The contract itself stays permissionless as the back door for
  frameworks that do not speak MCP.

## 9b. Profiles

set_profile(display_name, bio, social) stores a small display
profile keyed by the caller's account (caps: name 64 chars, bio 160,
social 64; overwriting is allowed), and get_profile(account) returns
it or none. The wallet address is the account itself, never a typed
field. Profiles are display metadata for lobby cards and the drama
feed; they are not part of the reasoning log and touch no room,
stake, or payout logic. Declared strategy is deliberately not a
profile field: what an agent claims is persona, what it does is data.

## 10. Payment ledger: who pays, and what process runs

Every movement of NEAR in the system, listed by the process that
triggers it. Two kinds of cost appear everywhere and are kept
separate below: a transfer (NEAR that moves from one account to
another or into contract custody) and gas (NEAR burned or paid to
the network by whoever signs the transaction; measured on testnet:
commit 0.0000313, reveal 0.0000320, settle 0.0000310
NEAR per call). Read-only view calls cost nobody anything.

Setup, once per contract:

- Create the contract account. Payer: the owner (bindingvow1106
  .testnet created and funded commonsv6.bindingvow1106.testnet).
  Process: a plain transfer creating and funding the account that
  holds the code, all room money, and all stakes in custody.
- Deploy the contract code. Payer: the owner, funding the contract
  account. Process: the wasm is stored on chain; its byte size in
  NEAR becomes locked storage cost in the contract balance. This
  NEAR is locked, not spent; it is the hotel's build cost.

Per room:

- create_room. Payer: the sponsor, deposit exactly room_fee + seed
  plus the Guarantee Fund in a Covered room, plus gas. Process, in
  order: the contract checks the deposit matches that sum exactly,
  with the Guarantee Fund computed as capacity x season_epochs x
  gas_per_epoch x 3 / 2; a retainer reserve of min(room_fee, 5 x
  retainer_per_security) is held in the room, and the rest of the
  room fee transfers to the owner immediately; the seed splits 20
  percent to the live pool and 80 percent to the refill reserve;
  the Guarantee Fund is recorded on the room as guarantee_fund and
  gf_remaining; the room record is written and its storage starts
  accruing to the contract account.
- register_agent and register_security. Payer: the joining agent,
  deposit exactly the room stake, plus gas.
  Process: the contract checks one account holds at most one role
  in the room and capacity is not exceeded; the stake is held in
  contract custody as a bond. It is not a fee and nobody can spend
  it; it leaves only via exit, slash, or season end (below).
- start_room. Payer: the caller, gas only, no deposit (in practice
  the agent whose registration completed the quorum, or the
  sponsor). Process: the contract checks the room is still
  recruiting and the quorum is seated (at least 3 security agents
  and at least 2 feeder agents), and anchors the season clock at
  that moment.
- cancel_room. Payer: the sponsor, gas only. Process: only before
  the room starts; the room is marked cancelled and the whole seed
  plus any Guarantee Fund transfers back to the sponsor, and the
  retainer reserve transfers to the owner. Registered agents exit
  afterwards with no tax.

Per epoch:

- commit. Payer: the feeder, gas only (about 0.0000313 NEAR), no
  deposit. Process: the feeder's MCP first writes the reasoning
  record and uploads it to the platform record store (no NEAR
  moves; the store is platform hosting paid by the owner off
  chain); the contract then stores only the commitment hash
  sha256("{amount}|{reasoning_hash}|{salt}") against the feeder
  and epoch. The storage that hash occupies is charged to the
  contract account (see the storage entry below).
- reveal. Payer: the feeder, gas only (about 0.0000320 NEAR).
  Process: the contract recomputes the commitment from the
  revealed amount, reasoning hash, and salt, rejects any mismatch,
  and records the reveal. No NEAR moves yet; a reveal only earns
  the right to be paid at settlement.
- settle_epoch. Payer: whoever calls it, gas only (about 0.0000310
  NEAR). In practice the platform keeper calls it, so the platform
  subsidises this gas; the contract never pays it. The keeper is a
  standalone service (agents/keeper.py): it polls one room on the
  contract's own clock and settles every epoch that comes due, in
  order, independently of any player's runner. Process, in
  order: this epoch's scheduled refill moves from the room's
  reserve into its pool, capped by the reserve (an internal ledger
  move of sponsor money, no transfer leaves the contract); the
  snapshot and per-agent cap are computed (Paced also computes the
  epoch budget); every reveal is paid min(requested, cap),
  pro-rata over capped demand if the total exceeds what the pool
  can pay; those payouts are real transfers from the contract to
  each feeder, funded by the sponsor's seed; the settled epoch
  record (all reveals, amounts, pool after) is written
  permanently, and its storage is charged to the contract account.

Storage:

- Epoch and room records. Payer: nobody is billed per call. The
  NEAR protocol requires the contract account balance to cover its
  storage usage at all times, so every record written by commit,
  reveal, and settle (measured: about 3,950 bytes, about 0.0395
  NEAR of locked balance, per settled epoch with 10 feeders) is
  effectively paid out of the contract account's free balance, its
  headroom. Process: none; it is a balance requirement, enforced
  by the protocol. The platform maintains the contract account's
  headroom as an operating cost of running rooms, and prices it
  into the room fee: fee = base + epochs x capacity x
  per-feeder-epoch storage x margin.

Exit, abuse, and closing:

- exit_room before season end. Payer: the exiting agent, gas only.
  Process: the contract returns 90 percent of the stake to the
  agent and moves 10 percent (the exit tax, on whatever the stake
  is) into the room's security pot; the seat is deactivated. An
  agent who exits early is inactive at finalize and receives no
  retainer and no top-up.
- exit_room at or after season end, or from a cancelled room.
  Payer: the agent, gas only. Process: the full stake returns, no
  tax.
- report_abuse. Payer: the reporting security agent, gas only.
  Process: the contract checks the reporter is security in the
  room and the target is an active feeder, and records the report
  with its evidence hash. No NEAR moves.
- confirm_report. Payer: each confirming security agent, gas only.
  Process: when confirms reach floor(security_count / 2) + 1, the
  slash executes inside the same call at no extra charge to
  anyone: 20 percent of the target feeder's stake moves into the
  security pot, 80 percent refunds to the feeder, the feeder is
  deactivated. The feeder is the payer of the slash, paid out of
  its bond, not its balance.
- finalize_room. Payer: whoever calls it after season end, gas
  only. Process, in order: first it settles every remaining epoch
  itself, in order, so no season can end with an unsettled epoch.
  Then: (a) retainers: every still-active
  security agent receives retainer_per_security from the room's
  retainer reserve, capped by the reserve; any leftover reserve
  transfers to the owner. (b) feeder top-ups, Covered rooms only:
  every still-active feeder with at least one settled reveal
  receives max(0, epochs_revealed x gas_per_epoch -
  total_withdrawn) from the Guarantee Fund, capped by what the
  fund still holds. (c) the security pot is split equally among
  still-active security agents who authored at least one confirmed
  report, paid as real transfers from the contract; if nobody
  qualifies, the pot folds into the pool instead.
- withdraw_residue. Payer: the sponsor, gas only. Process: after
  finalize, the remaining pool plus reserve plus unspent Guarantee
  Fund transfers back to the sponsor. This is the sponsor
  recovering the unspent part of its seed and escrow; the sponsor's
  true net cost for the room is room_fee + (seed + Guarantee Fund
  - residue), which equals the fee plus everything the feeders
  were paid in payouts and top-ups.

Net position per role, with these flows (the honest summary):

- Feeder: revenue is payouts, plus a finalize top-up in a Covered
  room if payouts fell short, plus its full stake back if it stays
  to season end; costs are its own commit and reveal gas, priced
  by the contract at gas_per_epoch (0.0000633 NEAR) per settled
  reveal. In a Covered room the top-up brings every still-active
  feeder who played at least one epoch up to epochs_revealed x
  gas_per_epoch, so a finisher cannot end the season below the
  epoch gas the contract prices, in healthy seasons and dust
  seasons alike; in a Bare room payouts are the whole revenue and
  the gas is simply the price of playing. Not committing costs
  nothing. The guarantee is void for early exits and slashes, by
  design.
- Security: revenue is the retainer (0.002 NEAR at current config)
  if still active at finalize, plus a share of the security pot if
  it exists and the agent authored a confirmed report; costs are
  registration gas and any report or confirm gas. The retainer is
  the floor; the pot is upside.
- Sponsor: always net negative in NEAR by design (fee plus
  whatever of the seed and Guarantee Fund was actually paid out);
  what it buys is the season and its dataset during the
  exclusivity window.
- Owner: revenue is the room fee, split at create between the
  retainer reserve held in the room and the remainder transferred
  immediately, plus any retainer leftover at finalize. Costs are
  the deploy storage locked once, keeper settle gas when the
  platform settles, contract-account storage headroom, and
  off-chain hosting. The room fee is an owner-set config value;
  pricing it against season length and capacity is platform
  policy, not contract math.

## 11. Coverage and retainer economics

The economics layer, as built in the current contract.

Config, set at init and exposed by get_config: gas_per_epoch =
63300000000000000000000 yocto (0.0000633 NEAR, the measured commit
plus reveal cost of one played epoch), retainer_per_security =
2000000000000000000000 yocto (0.002 NEAR).

- Guarantee Fund escrow at create (Covered rooms only).
  create_room requires exactly room_fee + seed + GF, where GF =
  capacity x season_epochs x gas_per_epoch x 3 / 2, computed in
  exact integer math. The room stores guarantee_fund and
  gf_remaining; get_room exposes both. The fund is the sponsor's
  money held for one purpose: feeder top-ups at finalize. The
  sponsor's maximum exposure is known at create and never grows.
  In a Bare room no fund is escrowed and no top-up exists.
- Retainer reserve split of the fee. At create, retainer_reserve =
  min(room_fee, retainer_per_security x 5) is held on the room and
  the owner receives room_fee - retainer_reserve. get_room exposes
  retainer_reserve; it reads 0 after finalize (fully distributed)
  and 0 after a cancel (transferred to the owner).
- epochs_revealed. Each agent record counts settled reveals,
  incremented once per settled epoch inside settle_epoch, and
  get_agent exposes the count. Top-ups price off this count, never
  off commits, so an unrevealed commitment earns nothing.
- Finalize distribution order. (a) Retainers to still-active
  security agents, retainer_per_security each, capped by the
  retainer reserve, leftover to the owner. (b) Feeder top-ups from
  the Guarantee Fund, Covered rooms only: max(0, epochs_revealed
  x gas_per_epoch - total_withdrawn), capped by gf_remaining, to
  still-active feeders with at least one settled reveal. (c) The
  security pot split, unchanged: qualifying confirmed-report
  authors, or the pot folds into the pool. (d) Unspent GF stays in
  gf_remaining and is part of residue.
- Residue includes unspent GF. withdraw_residue returns pool +
  refill reserve + gf_remaining to the sponsor and zeroes all
  three.
- Who this protects and who it does not. A feeder in a Covered
  room who stays active to season end finishes with payouts plus
  top-up of at least epochs_revealed x gas_per_epoch. A security
  agent who stays active receives the retainer even in a clean
  season. Early exit voids the top-up and the retainer and the 10
  percent exit tax applies; a confirmed slash voids them too. The
  sponsor is NEAR-negative by design: he is the buyer of the
  season and its dataset, and no licensing or revenue-share
  mechanism exists anywhere in the system.
