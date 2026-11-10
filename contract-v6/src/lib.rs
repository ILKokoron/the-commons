//! The Commons v5: a shared pool that refills slowly, fed on by AI
//! agents from different operators (testnet).
//!
//! A sponsor funds a room (seed plus a flat room fee to the contract
//! owner). Operators register one agent per room by locking a stake that
//! acts as a bond, never as food. Time runs in epochs. In each epoch an
//! agent first commits a hash of its intended withdrawal, then reveals
//! the amount together with the hash of its reasoning record. A hard
//! per-epoch cap (basis points of the current pool) is enforced by the
//! contract, so no agent can ever take the whole pool at once. At
//! settlement the pool is refilled from the room reserve, then reveals
//! are paid; if valid demand exceeds the pool, payment is pro-rata.
//!
//! Leaving before the season ends costs a 10 percent exit tax on the
//! stake. The tax accumulates in the room's security pot, which is split
//! at season end among the room's security agents who authored at least
//! one confirmed abuse report. A confirmed report (majority of the
//! room's security agents) slashes 20 percent of the reported feeder's
//! stake into the pot and deactivates the feeder.
//!
//! Every reveal anchors a reasoning record by hash: the dataset of who
//! withdrew what, when, and why is the product this contract produces.
//!
//! What v2 adds over v1 (everything else is behavior-identical):
//! - Regime preset per room. Wild settles exactly like v1. Paced caps
//!   the total payout of each epoch at the post-refill pool snapshot
//!   divided by the epochs remaining in the season, so under maximum
//!   greed the pool declines linearly and reaches zero exactly at the
//!   final epoch (whose budget is the whole remaining snapshot).
//! - Sponsor start trigger. A room is created in a recruiting state
//!   with no season clock running. Registration is open during
//!   recruiting; commit, reveal and settle are rejected until the
//!   sponsor (and only the sponsor) calls start_room, which anchors
//!   epoch 0 at that moment.
//! - Start gate: start_room requires at least 3 registered security
//!   agents, so abuse reports can actually reach their confirm
//!   threshold in every started room.
//! - Stake-zero rooms: the room stake may be 0 (gas-only entry tiers).
//! - Profile store: set_profile / get_profile hold small display
//!   metadata keyed by account. Profiles touch nothing else.
//!
//! What v3 adds over v2 (everything else is behavior-identical):
//! - No-loss economics (ECONOMICS-V3-PROPOSAL.md, adopted 2026-11-08).
//!   create_room escrows a Guarantee Fund on top of fee + seed:
//!   GF = capacity x season_epochs x gas_per_epoch x 3 / 2. At
//!   finalize, every still-active feeder is topped up from the fund
//!   to epochs_revealed x gas_per_epoch + topup_eps if their pool
//!   payouts fell short, so a feeder who plays to the end cannot end
//!   the season below the gas they spent. Unspent GF returns to the
//!   sponsor inside withdraw_residue.
//! - Security retainer: a slice of the room fee (up to 5 x
//!   retainer_per_security) is held per room at create instead of
//!   going straight to the owner; at finalize every still-active
//!   security agent receives retainer_per_security from it, so a
//!   clean season no longer leaves security negative on gas. Any
//!   leftover goes to the owner at finalize.
//! - Agent records count epochs_revealed (settled reveals), which
//!   the top-up formula and get_agent expose.
//! The sponsor is NEAR-negative by design (he buys the dataset);
//! no licensing or revenue-share mechanism exists anywhere.
//!
//! What v4 adds over v3 (everything else is behavior-identical):
//! - Coverage choice at create (SCHEME section 8, locked
//!   2026-11-09). A room is Bare (no Guarantee Fund at all: the
//!   deposit is fee + seed, and finalize pays no top-ups; losses
//!   are real) or Covered (the v3 fund, but the finalize top-up
//!   target is gas only:
//!   epochs_revealed x gas_per_epoch - total_withdrawn, floored at
//!   zero). The v3 flat topup_eps is REMOVED from the target: it
//!   was a profit sweetener, not loss protection (an outside
//!   operator's formal model showed it made lazy restraint the
//!   dominant strategy, was farmable by stake-zero sybils, and was
//!   the term the fund did not cover in short seasons). (In v6
//!   the stored config value itself is deleted; see below.) With a gas-only target the fund
//!   (capacity x season_epochs x gas x 3/2) covers the worst case
//!   at every season length.
//! - Permissionless start and a cancel path. start_room may be
//!   called by ANY account once the room is in Recruiting with at
//!   least MIN_SECURITY_TO_START security seated (the sponsor-only
//!   rule is gone; v2 made the sponsor a single point of failure
//!   for rooms that were ready to run). v5 adds the player quorum:
//!   at least MIN_FEEDERS_TO_START feeders must also be seated, so
//!   an empty or one-agent room stays in Recruiting, visibly
//!   waiting for players, until the quorum completes. New cancel_room: the
//!   sponsor, and only while the room is still in Recruiting, can
//!   close the room; seed + GF return to the sponsor (the room fee
//!   is not refunded: it is the platform's price for the slot) and
//!   registered agents can then exit through the normal exit path
//!   with NO exit tax, because nothing was ever played.
//! - Refill modes at create (SCHEME section 8). Flat is the v3
//!   behaviour. Decaying pays the room's current_refill each epoch
//!   and then decays it: current_refill becomes
//!   max(refill_floor, current_refill x decay_bps / 10000) for the
//!   next epoch, so the tap dries over the season but is floored.
//!   Still reserve-based: decaying never mints, it only stretches
//!   the reserve.
//!
//! What v6 changes over v5 (the audit round; behavior is
//! otherwise identical):
//! - Amounts are bounded at the reveal door (MAX_REVEAL_YOCTO)
//!   and settle accumulates with checked arithmetic, so one
//!   malicious reveal can no longer overflow a settle and brick
//!   the room (A1).
//! - finalize_room settles every remaining epoch itself before
//!   distributing; no season can end with unsettled epochs (A2).
//! - Abuse accounting: the confirm threshold is snapshotted on
//!   the report at filing time, the reporter is credited only
//!   when a slash actually happens, and reports/confirms reject
//!   ended or cancelled rooms so a slash can never strand in a
//!   distributed pot (A3).
//! - Duplicate commits reject instead of overwriting; settling
//!   a ghost epoch (>= season_epochs) rejects; a cancelled room
//!   releases its retainer reserve to the owner instead of
//!   stranding it; exit permanence is documented on exit_room;
//!   room name, epoch_secs and season_epochs are range-bound at
//!   create (A4).
//! - The inert topup_eps config value is deleted outright, and
//!   data exclusivity is no longer contract state: the window is
//!   a platform-terms setting kept with the record store, not on
//!   chain.

use near_sdk::borsh::{self, BorshDeserialize, BorshSerialize};
use near_sdk::json_types::U128;
use near_sdk::serde::{Deserialize, Serialize};
use near_sdk::store::{IterableMap, LookupMap};
use near_sdk::{env, near, AccountId, NearToken, PanicOnDefault, Promise};
use schemars::JsonSchema;

pub const EXIT_TAX_BPS: u128 = 1_000; // 10% of stake on early exit
pub const ABUSE_SLASH_BPS: u128 = 2_000; // 20% of a confirmed abuser's stake
pub const INITIAL_POOL_BPS: u128 = 2_000; // 20% of the seed starts in the pool
pub const BPS_DENOMINATOR: u128 = 10_000;
pub const NS_PER_SEC: u64 = 1_000_000_000;
pub const MIN_SECURITY_TO_START: u32 = 3;
// v5: a room also needs players. Below this many seated feeders the
// room stays in Recruiting (waiting for players) and start_room
// rejects every caller, the sponsor included.
pub const MIN_FEEDERS_TO_START: u32 = 2;
// v3 economics config, set at init (see Commons::new). The values are
// the measured testnet costs from ECONOMICS-V3-PROPOSAL.md section 1.
pub const GAS_PER_EPOCH: u128 = 63_300_000_000_000_000_000; // 0.0000633 NEAR
// v6 input bounds (audit A1/A4). A reveal may ask for at most
// MAX_REVEAL_YOCTO; with capacity also bounded at create, the
// settle accumulators can never approach the u128 ceiling, and
// they use checked arithmetic on top of that.
pub const MAX_REVEAL_YOCTO: u128 = 1_000_000_000_000_000_000_000_000_000_000; // 10^30 yocto = 1,000,000 NEAR
pub const ROOM_NAME_MAX: usize = 64;
pub const HASH_STR_MAX: usize = 128;
pub const LOG_REF_MAX: usize = 512;
pub const EPOCH_SECS_MIN: u64 = 30;
pub const EPOCH_SECS_MAX: u64 = 86_400;
pub const SEASON_EPOCHS_MAX: u64 = 5_000;
pub const RETAINER_PER_SECURITY: u128 = 2_000_000_000_000_000_000_000; // 0.002 NEAR
pub const MAX_RETAINERS_PER_ROOM: u128 = 5;
pub const PROFILE_NAME_MAX: usize = 64;
pub const PROFILE_BIO_MAX: usize = 160;
pub const PROFILE_SOCIAL_MAX: usize = 64;

#[derive(BorshSerialize, BorshDeserialize, Clone, Copy, PartialEq, Debug)]
pub enum Role {
    Feeder,
    Security,
}

pub fn role_str(r: Role) -> &'static str {
    match r {
        Role::Feeder => "feeder",
        Role::Security => "security",
    }
}

#[derive(
    BorshSerialize,
    BorshDeserialize,
    Serialize,
    Deserialize,
    Clone,
    Copy,
    PartialEq,
    Debug,
    JsonSchema,
)]
#[serde(crate = "near_sdk::serde")]
pub enum Regime {
    Wild,
    Paced,
}

pub fn regime_str(r: Regime) -> &'static str {
    match r {
        Regime::Wild => "wild",
        Regime::Paced => "paced",
    }
}

/// v4: whether the room carries a Guarantee Fund (SCHEME section
/// 8, Coverage). Bare is the pure form (no fund, no top-ups);
/// Covered escrows the fund and tops finishers up to their epoch
/// gas at finalize.
#[derive(
    BorshSerialize,
    BorshDeserialize,
    Serialize,
    Deserialize,
    Clone,
    Copy,
    PartialEq,
    Debug,
    JsonSchema,
)]
#[serde(crate = "near_sdk::serde")]
pub enum Coverage {
    Bare,
    Covered,
}

pub fn coverage_str(c: Coverage) -> &'static str {
    match c {
        Coverage::Bare => "bare",
        Coverage::Covered => "covered",
    }
}

/// v4: the refill schedule (SCHEME section 8, Refill mode). Flat
/// pays refill_per_epoch every epoch, as v1-v3. Decaying pays the
/// room's current_refill, which shrinks by decay_bps each epoch
/// down to refill_floor.
#[derive(
    BorshSerialize,
    BorshDeserialize,
    Serialize,
    Deserialize,
    Clone,
    Copy,
    PartialEq,
    Debug,
    JsonSchema,
)]
#[serde(crate = "near_sdk::serde")]
pub enum RefillMode {
    Flat,
    Decaying,
}

pub fn refill_mode_str(m: RefillMode) -> &'static str {
    match m {
        RefillMode::Flat => "flat",
        RefillMode::Decaying => "decaying",
    }
}

pub const DEFAULT_DECAY_BPS: u32 = 9_800; // 0.98 per epoch

#[derive(BorshSerialize, BorshDeserialize, Clone)]
pub struct Room {
    pub id: u64,
    pub sponsor: AccountId,
    pub name: String,
    pub regime: Regime,
    pub stake: u128,
    pub capacity: u32,
    pub feeders_count: u32,
    pub security_count: u32,
    pub pool_balance: u128,
    pub refill_reserve: u128,
    pub refill_per_epoch: u128,
    pub cap_bps: u32,
    pub epoch_secs: u64,
    pub season_epochs: u64,
    pub started: bool,
    pub start_ns: u64,
    pub security_pot: u128,
    pub total_paid_out: u128,
    pub next_epoch_to_settle: u64,
    pub ended: bool,
    pub pot_distributed: bool,
    pub residue_withdrawn: bool,
    // v3 economics: the Guarantee Fund escrowed at create, what is
    // left of it, and the room-fee slice held back for security
    // retainers (zeroed at finalize once fully distributed).
    pub guarantee_fund: u128,
    pub gf_remaining: u128,
    pub retainer_reserve: u128,
    // v4: the coverage choice (Bare rooms keep guarantee_fund and
    // gf_remaining at 0 forever).
    pub coverage: Coverage,
    // v4: the refill schedule. current_refill is what the next
    // epoch will pay; it starts at refill_per_epoch and, in a
    // Decaying room, is decayed after every settled epoch down to
    // refill_floor. In a Flat room it never changes.
    pub refill_mode: RefillMode,
    pub decay_bps: u32,
    pub refill_floor: u128,
    pub current_refill: u128,
    // v4: set by cancel_room while the room is still in
    // Recruiting. A cancelled room can never start, and its
    // registered agents exit with no exit tax.
    pub cancelled: bool,
}

#[derive(BorshSerialize, BorshDeserialize, Clone)]
pub struct AgentRec {
    pub role: Role,
    pub stake: u128,
    pub active: bool,
    pub total_withdrawn: u128,
    pub confirmed_reports_authored: u32,
    // v3: settled reveals, counted once per settled epoch in
    // settle_epoch. The finalize top-up is priced off this count.
    pub epochs_revealed: u32,
}

#[derive(BorshSerialize, BorshDeserialize, Clone)]
pub struct RevealRec {
    pub amount: u128,
    pub reasoning_hash: String,
    pub log_ref: String,
    pub paid: u128,
}

#[derive(BorshSerialize, BorshDeserialize, Clone)]
pub struct EpochRec {
    pub epoch: u64,
    pub total_requested: u128,
    pub total_paid: u128,
    pub refill_moved: u128,
    pub pool_after: u128,
    pub reveal_count: u32,
}

#[derive(BorshSerialize, BorshDeserialize, Clone)]
pub struct Report {
    pub id: u64,
    pub room_id: u64,
    pub reporter: AccountId,
    pub target: AccountId,
    pub evidence_hash: String,
    pub confirms: Vec<AccountId>,
    pub confirmed: bool,
    // v6 (audit A3): the confirm threshold is snapshotted at
    // report time from the room's security count then seated.
    // Security registration stays open all season, so computing
    // the threshold at confirm time would let late registrations
    // (or exits) move the bar under a live report.
    pub threshold: u32,
}

#[derive(BorshSerialize, BorshDeserialize, Clone)]
pub struct Profile {
    pub display_name: String,
    pub bio: String,
    pub social: String,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct Commons {
    pub owner: AccountId,
    pub room_fee: u128,
    // v3 economics config, set at init, owner-governed.
    pub gas_per_epoch: u128,
    pub retainer_per_security: u128,
    pub next_room_id: u64,
    pub next_report_id: u64,
    pub rooms: IterableMap<u64, Room>,
    pub agents: LookupMap<(u64, AccountId), AgentRec>,
    pub commits: LookupMap<(u64, u64, AccountId), String>,
    pub reveals: LookupMap<(u64, u64, AccountId), RevealRec>,
    pub revealers: LookupMap<(u64, u64), Vec<AccountId>>,
    pub epochs: LookupMap<(u64, u64), EpochRec>,
    pub reports: IterableMap<u64, Report>,
    pub profiles: LookupMap<AccountId, Profile>,
    // v3: every registered account per room, in registration order.
    // agents is a LookupMap and cannot be iterated, and finalize_room
    // must visit every security agent (retainers) and every feeder
    // (top-ups) of the room, including ones that never appear in any
    // report.
    pub rosters: LookupMap<u64, Vec<AccountId>>,
}

// ---------- views ----------

#[derive(Serialize, Deserialize, JsonSchema)]
#[serde(crate = "near_sdk::serde")]
pub struct ConfigView {
    pub owner: String,
    pub room_fee: String,
    pub exit_tax_bps: u32,
    pub abuse_slash_bps: u32,
    pub rooms_created: u64,
    pub gas_per_epoch: String,
    pub retainer_per_security: String,
}

#[derive(Serialize, Deserialize, JsonSchema)]
#[serde(crate = "near_sdk::serde")]
pub struct RoomView {
    pub id: u64,
    pub sponsor: String,
    pub name: String,
    pub regime: String,
    pub stake: String,
    pub capacity: u32,
    pub feeders_count: u32,
    pub security_count: u32,
    pub pool_balance: String,
    pub refill_reserve: String,
    pub refill_per_epoch: String,
    pub cap_bps: u32,
    pub epoch_secs: u64,
    pub season_epochs: u64,
    pub started: bool,
    pub start_ns: String,
    pub security_pot: String,
    pub total_paid_out: String,
    pub next_epoch_to_settle: u64,
    pub ended: bool,
    pub guarantee_fund: String,
    pub gf_remaining: String,
    pub retainer_reserve: String,
    pub coverage: String,
    pub refill_mode: String,
    pub decay_bps: u32,
    pub refill_floor: String,
    pub current_refill: String,
    pub cancelled: bool,
}

#[derive(Serialize, Deserialize, JsonSchema)]
#[serde(crate = "near_sdk::serde")]
pub struct AgentView {
    pub account: String,
    pub role: String,
    pub stake: String,
    pub active: bool,
    pub total_withdrawn: String,
    pub confirmed_reports_authored: u32,
    pub epochs_revealed: u32,
}

#[derive(Serialize, Deserialize, JsonSchema)]
#[serde(crate = "near_sdk::serde")]
pub struct RevealView {
    pub account: String,
    pub amount: String,
    pub reasoning_hash: String,
    pub log_ref: String,
    pub paid: String,
}

#[derive(Serialize, Deserialize, JsonSchema)]
#[serde(crate = "near_sdk::serde")]
pub struct EpochView {
    pub room_id: u64,
    pub epoch: u64,
    pub total_requested: String,
    pub total_paid: String,
    pub refill_moved: String,
    pub pool_after: String,
    pub reveal_count: u32,
    pub reveals: Vec<RevealView>,
}

#[derive(Serialize, Deserialize, JsonSchema)]
#[serde(crate = "near_sdk::serde")]
pub struct EpochInfo {
    pub room_id: u64,
    pub epoch: u64,
    pub phase: String,
    pub seconds_into_epoch: u64,
    pub season_over: bool,
    pub started: bool,
}

#[derive(Serialize, Deserialize, JsonSchema)]
#[serde(crate = "near_sdk::serde")]
pub struct ReportView {
    pub id: u64,
    pub room_id: u64,
    pub reporter: String,
    pub target: String,
    pub evidence_hash: String,
    pub confirms: u32,
    pub confirmed: bool,
}

#[derive(Serialize, Deserialize, JsonSchema)]
#[serde(crate = "near_sdk::serde")]
pub struct ProfileView {
    pub account: String,
    pub display_name: String,
    pub bio: String,
    pub social: String,
}

fn room_view(r: &Room) -> RoomView {
    RoomView {
        id: r.id,
        sponsor: r.sponsor.to_string(),
        name: r.name.clone(),
        regime: regime_str(r.regime).to_string(),
        stake: r.stake.to_string(),
        capacity: r.capacity,
        feeders_count: r.feeders_count,
        security_count: r.security_count,
        pool_balance: r.pool_balance.to_string(),
        refill_reserve: r.refill_reserve.to_string(),
        refill_per_epoch: r.refill_per_epoch.to_string(),
        cap_bps: r.cap_bps,
        epoch_secs: r.epoch_secs,
        season_epochs: r.season_epochs,
        started: r.started,
        start_ns: r.start_ns.to_string(),
        security_pot: r.security_pot.to_string(),
        total_paid_out: r.total_paid_out.to_string(),
        next_epoch_to_settle: r.next_epoch_to_settle,
        ended: r.ended,
        guarantee_fund: r.guarantee_fund.to_string(),
        gf_remaining: r.gf_remaining.to_string(),
        retainer_reserve: r.retainer_reserve.to_string(),
        coverage: coverage_str(r.coverage).to_string(),
        refill_mode: refill_mode_str(r.refill_mode).to_string(),
        decay_bps: r.decay_bps,
        refill_floor: r.refill_floor.to_string(),
        current_refill: r.current_refill.to_string(),
        cancelled: r.cancelled,
    }
}

fn hex_encode(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

pub fn commitment_for(amount: u128, reasoning_hash: &str, salt: &str) -> String {
    let preimage = format!("{}|{}|{}", amount, reasoning_hash, salt);
    hex_encode(&env::sha256(preimage.as_bytes()))
}

/// floor(a * b / d) computed exactly, for 0 <= a <= d, 0 <= b <= d and
/// d > 0, by iterating the bits of b and doubling a running
/// quotient/remainder pair. A direct u128 product of two pool-scale
/// yocto amounts overflows (1e22 * 1e21 is about 1e43, far past the
/// u128 maximum of about 3.4e38), which is exactly the pro-rata case
/// at settlement. Every intermediate here stays below 2 * d, and every
/// amount in this contract is bounded by the total NEAR supply in
/// yocto (under 2^111), so no intermediate overflows u128 either.
pub fn mul_div_floor(a: u128, b: u128, d: u128) -> u128 {
    debug_assert!(d > 0 && a <= d && b <= d);
    let mut q: u128 = 0;
    let mut r: u128 = 0;
    for i in (0..128).rev() {
        // Double the running value q * d + r.
        q <<= 1;
        r <<= 1;
        if r >= d {
            r -= d;
            q += 1;
        }
        // Add a when this bit of b is set.
        if (b >> i) & 1 == 1 {
            r += a;
            if r >= d {
                r -= d;
                q += 1;
            }
        }
    }
    q
}

/// The Guarantee Fund escrowed at create_room:
/// GF = capacity x season_epochs x gas_per_epoch x 3 / 2, in exact
/// integer math. capacity is at most 1000 (asserted in create_room)
/// and gas_per_epoch is about 6.33e19 yocto, so for any season length
/// that fits a u64 the products stay far below the u128 maximum; the
/// checked multiplies turn a pathological input into a clear panic
/// instead of a silent wrap.
pub fn guarantee_fund_for(capacity: u32, season_epochs: u64, gas_per_epoch: u128) -> u128 {
    let plays = (capacity as u128) * (season_epochs as u128);
    let base = plays
        .checked_mul(gas_per_epoch)
        .expect("guarantee fund overflow");
    base.checked_mul(3).expect("guarantee fund overflow") / 2
}

#[near]
impl Commons {
    #[init]
    pub fn new(owner: AccountId, room_fee: U128) -> Self {
        Self {
            owner,
            room_fee: room_fee.0,
            gas_per_epoch: GAS_PER_EPOCH,
            retainer_per_security: RETAINER_PER_SECURITY,
            next_room_id: 0,
            next_report_id: 0,
            rooms: IterableMap::new(b"r"),
            agents: LookupMap::new(b"a"),
            commits: LookupMap::new(b"c"),
            reveals: LookupMap::new(b"v"),
            revealers: LookupMap::new(b"e"),
            epochs: LookupMap::new(b"p"),
            reports: IterableMap::new(b"t"),
            profiles: LookupMap::new(b"f"),
            rosters: LookupMap::new(b"o"),
        }
    }

    // ---------- rooms ----------

    #[payable]
    pub fn create_room(
        &mut self,
        name: String,
        regime: Regime,
        stake: U128,
        capacity: u32,
        seed: U128,
        refill_per_epoch: U128,
        cap_bps: u32,
        epoch_secs: u64,
        season_epochs: u64,
        coverage: Coverage,
        refill_mode: RefillMode,
        decay_bps: u32,
        refill_floor: U128,
    ) -> u64 {
        assert!(capacity >= 1 && capacity <= 1000, "capacity out of range");
        assert!(cap_bps >= 1 && (cap_bps as u128) <= BPS_DENOMINATOR, "cap_bps out of range");
        // v6 (audit A4): input ranges are bounded at the door.
        // epoch_secs and season_epochs size the season's time
        // arithmetic; the room name is stored and rendered.
        assert!(
            name.chars().count() <= ROOM_NAME_MAX,
            "room name too long (max 64 chars)"
        );
        assert!(
            epoch_secs >= EPOCH_SECS_MIN && epoch_secs <= EPOCH_SECS_MAX,
            "epoch_secs out of range (30 to 86400)"
        );
        assert!(
            season_epochs >= 1 && season_epochs <= SEASON_EPOCHS_MAX,
            "season_epochs out of range (1 to 5000)"
        );
        // v2: stake may be 0 (gas-only entry tiers). The seed must
        // still be positive: a room with no pool is not a room.
        assert!(seed.0 > 0, "seed must be positive");
        // v4 refill schedule knobs. decay_bps is only meaningful
        // for a Decaying room, where it is a per-epoch multiplier
        // and must be below 1 (the working default is
        // DEFAULT_DECAY_BPS = 9800, chosen by the caller).
        assert!(
            (decay_bps as u128) <= BPS_DENOMINATOR,
            "decay_bps out of range"
        );
        if matches!(refill_mode, RefillMode::Decaying) {
            assert!(
                (decay_bps as u128) < BPS_DENOMINATOR,
                "decay_bps must be below 10000 for a Decaying room"
            );
        }
        // v4 coverage: a Covered room escrows the Guarantee Fund
        // exactly as v3 did; a Bare room escrows nothing, so its
        // deposit is fee + seed only and finalize will pay no
        // top-ups. Either way the sponsor's worst case is fully
        // known at create and nothing more can ever be charged.
        let guarantee_fund = match coverage {
            Coverage::Covered => {
                guarantee_fund_for(capacity, season_epochs, self.gas_per_epoch)
            }
            Coverage::Bare => 0,
        };
        let deposit = env::attached_deposit().as_yoctonear();
        assert_eq!(
            deposit,
            self.room_fee + seed.0 + guarantee_fund,
            "deposit must equal room_fee + seed + guarantee_fund"
        );
        // v3 fee split: a retainer reserve of up to
        // MAX_RETAINERS_PER_ROOM x retainer_per_security is held in
        // this room and paid to active security agents at finalize;
        // the rest of the fee goes to the owner immediately, exactly
        // as the whole fee did in v1 and v2.
        let retainer_reserve =
            (self.retainer_per_security * MAX_RETAINERS_PER_ROOM).min(self.room_fee);
        let owner_fee = self.room_fee - retainer_reserve;
        if owner_fee > 0 {
            Promise::new(self.owner.clone()).transfer(NearToken::from_yoctonear(owner_fee));
        }
        let pool_start = seed.0 * INITIAL_POOL_BPS / BPS_DENOMINATOR;
        let id = self.next_room_id;
        self.next_room_id += 1;
        let room = Room {
            id,
            sponsor: env::predecessor_account_id(),
            name,
            regime,
            stake: stake.0,
            capacity,
            feeders_count: 0,
            security_count: 0,
            pool_balance: pool_start,
            refill_reserve: seed.0 - pool_start,
            refill_per_epoch: refill_per_epoch.0,
            cap_bps,
            epoch_secs,
            season_epochs,
            // v2: the room starts in the recruiting state. The season
            // clock is anchored by start_room, not by creation.
            started: false,
            start_ns: 0,
            security_pot: 0,
            total_paid_out: 0,
            next_epoch_to_settle: 0,
            ended: false,
            pot_distributed: false,
            residue_withdrawn: false,
            guarantee_fund,
            gf_remaining: guarantee_fund,
            retainer_reserve,
            coverage,
            refill_mode,
            decay_bps,
            refill_floor: refill_floor.0,
            current_refill: refill_per_epoch.0,
            cancelled: false,
        };
        self.rooms.insert(id, room);
        env::log_str(&format!("COMMONS room_created id={}", id));
        id
    }

    /// Anchor epoch 0 at the current moment and open the season.
    /// v4: ANY account may call this once the room is in Recruiting
    /// with at least MIN_SECURITY_TO_START registered security
    /// agents (with fewer, no abuse report in this room could ever
    /// reach its confirm threshold). The v2/v3 sponsor-only rule
    /// made the sponsor a single point of failure: a ready room
    /// whose sponsor went offline could never start.
    /// v5: the quorum also needs players. With fewer than
    /// MIN_FEEDERS_TO_START feeders seated, start_room rejects
    /// every caller and the room stays in Recruiting, waiting for
    /// players. In practice the agent whose registration completes
    /// the quorum fires this call, so a full room starts itself.
    pub fn start_room(&mut self, room_id: u64) {
        let mut room = self.rooms.get(&room_id).expect("room not found").clone();
        assert!(!room.cancelled, "room cancelled");
        assert!(!room.ended, "room ended");
        assert!(!room.started, "room already started");
        assert!(
            room.security_count >= MIN_SECURITY_TO_START,
            "need at least {} security agents to start, have {}",
            MIN_SECURITY_TO_START,
            room.security_count
        );
        assert!(
            room.feeders_count >= MIN_FEEDERS_TO_START,
            "need at least {} feeder agents to start, have {}",
            MIN_FEEDERS_TO_START,
            room.feeders_count
        );
        let now = env::block_timestamp();
        room.started = true;
        room.start_ns = now;
        self.rooms.insert(room_id, room);
        env::log_str(&format!("COMMONS room_started id={}", room_id));
    }

    /// v4: close a room that never started. Sponsor only, and only
    /// while the room is still in Recruiting. The seed and, for a
    /// Covered room, the Guarantee Fund return to the sponsor in
    /// full (nothing was ever paid out of them); the room fee is
    /// NOT refunded, it is the platform's price for the slot.
    /// Registered agents keep their records and can exit through
    /// exit_room, which charges no exit tax on a cancelled room:
    /// nothing was ever played, so there is nothing to tax.
    pub fn cancel_room(&mut self, room_id: u64) {
        let caller = env::predecessor_account_id();
        let mut room = self.rooms.get(&room_id).expect("room not found").clone();
        assert_eq!(caller, room.sponsor, "only the sponsor can cancel the room");
        assert!(!room.cancelled, "room already cancelled");
        assert!(!room.ended, "room ended");
        assert!(!room.started, "room already started");
        room.cancelled = true;
        // v6 (audit A4): the retainer reserve must not strand in a
        // cancelled room. It was carved out of the room fee to pay
        // security at finalize; a cancelled room never finalizes
        // and no security ever serves, so the held-back slice
        // completes the fee to the owner here.
        if room.retainer_reserve > 0 {
            let reserve = room.retainer_reserve;
            room.retainer_reserve = 0;
            Promise::new(self.owner.clone())
                .transfer(NearToken::from_yoctonear(reserve));
        }
        // Everything the room still holds for the sponsor: pool +
        // reserve is the whole seed (no epoch ever settled), plus
        // whatever the Guarantee Fund holds (all of it, or 0 in a
        // Bare room).
        let refund = room.pool_balance + room.refill_reserve + room.gf_remaining;
        room.pool_balance = 0;
        room.refill_reserve = 0;
        room.gf_remaining = 0;
        let sponsor = room.sponsor.clone();
        self.rooms.insert(room_id, room);
        if refund > 0 {
            Promise::new(sponsor).transfer(NearToken::from_yoctonear(refund));
        }
        env::log_str(&format!("COMMONS room_cancelled id={}", room_id));
    }

    // ---------- registration ----------

    #[payable]
    pub fn register_agent(&mut self, room_id: u64) {
        self.register(room_id, Role::Feeder);
    }

    #[payable]
    pub fn register_security(&mut self, room_id: u64) {
        self.register(room_id, Role::Security);
    }

    fn register(&mut self, room_id: u64, role: Role) {
        let caller = env::predecessor_account_id();
        let mut room = self.rooms.get(&room_id).expect("room not found").clone();
        assert!(!room.ended, "room ended");
        // v4: a cancelled room takes no registrations.
        assert!(!room.cancelled, "room cancelled");
        // Registration is open during recruiting (before start) and,
        // as in v1, mid-season up to capacity while the season runs.
        if room.started {
            assert!(
                env::block_timestamp() < self.season_end_ns(&room),
                "season over"
            );
        }
        assert!(
            self.agents.get(&(room_id, caller.clone())).is_none(),
            "already registered in this room"
        );
        let deposit = env::attached_deposit().as_yoctonear();
        assert_eq!(deposit, room.stake, "deposit must equal the room stake");
        match role {
            Role::Feeder => {
                assert!(room.feeders_count < room.capacity, "room full");
                room.feeders_count += 1;
            }
            Role::Security => {
                room.security_count += 1;
            }
        }
        self.rooms.insert(room_id, room);
        // v3: record the room roster (see the rosters field).
        let mut roster = self
            .rosters
            .get(&room_id)
            .cloned()
            .unwrap_or_default();
        roster.push(caller.clone());
        self.rosters.insert(room_id, roster);
        self.agents.insert(
            (room_id, caller),
            AgentRec {
                role,
                stake: deposit,
                active: true,
                total_withdrawn: 0,
                confirmed_reports_authored: 0,
                epochs_revealed: 0,
            },
        );
    }

    // ---------- epoch flow ----------

    fn season_end_ns(&self, room: &Room) -> u64 {
        room.start_ns + room.epoch_secs * NS_PER_SEC * room.season_epochs
    }

    fn season_over(&self, room: &Room, now: u64) -> bool {
        room.started && now >= self.season_end_ns(room)
    }

    fn epoch_index(&self, room: &Room, now: u64) -> u64 {
        if now <= room.start_ns {
            return 0;
        }
        (now - room.start_ns) / (room.epoch_secs * NS_PER_SEC)
    }

    fn phase_of(&self, room: &Room, now: u64) -> (u64, bool, u64) {
        let epoch_len = room.epoch_secs * NS_PER_SEC;
        let elapsed = now.saturating_sub(room.start_ns);
        let epoch = elapsed / epoch_len;
        let into = elapsed % epoch_len;
        let is_commit = into < epoch_len / 2;
        (epoch, is_commit, into / NS_PER_SEC)
    }

    pub fn current_epoch(&self, room_id: u64) -> EpochInfo {
        let room = self.rooms.get(&room_id).expect("room not found");
        if !room.started {
            return EpochInfo {
                room_id,
                epoch: 0,
                phase: "recruiting".to_string(),
                seconds_into_epoch: 0,
                season_over: false,
                started: false,
            };
        }
        let now = env::block_timestamp();
        let (epoch, is_commit, secs) = self.phase_of(room, now);
        EpochInfo {
            room_id,
            epoch,
            phase: if now >= self.season_end_ns(room) {
                "over".to_string()
            } else if is_commit {
                "commit".to_string()
            } else {
                "reveal".to_string()
            },
            seconds_into_epoch: secs,
            season_over: now >= self.season_end_ns(room),
            started: true,
        }
    }

    pub fn commit(&mut self, room_id: u64, commitment: String) {
        let caller = env::predecessor_account_id();
        let room = self.rooms.get(&room_id).expect("room not found");
        let agent = self
            .agents
            .get(&(room_id, caller.clone()))
            .expect("not registered in this room");
        assert!(agent.active, "agent inactive");
        assert!(matches!(agent.role, Role::Feeder), "only feeders commit");
        assert!(room.started, "room not started");
        let now = env::block_timestamp();
        assert!(now < self.season_end_ns(room), "season over");
        let (epoch, is_commit, _) = self.phase_of(room, now);
        assert!(is_commit, "not in commit phase");
        // v6 (audit A4): a second commit in the same epoch used to
        // silently overwrite the first (reveal already rejected
        // duplicates; commit did not). Overwriting would let an
        // agent rewrite a decision after seeing others move, which
        // is exactly what commit-reveal exists to prevent.
        assert!(
            commitment.len() <= HASH_STR_MAX,
            "commitment string too long"
        );
        assert!(
            self.commits.get(&(room_id, epoch, caller.clone())).is_none(),
            "already committed for this epoch"
        );
        self.commits.insert((room_id, epoch, caller), commitment);
    }

    pub fn reveal(
        &mut self,
        room_id: u64,
        amount: U128,
        reasoning_hash: String,
        log_ref: String,
        salt: String,
    ) {
        let caller = env::predecessor_account_id();
        let room = self.rooms.get(&room_id).expect("room not found");
        let agent = self
            .agents
            .get(&(room_id, caller.clone()))
            .expect("not registered in this room");
        assert!(agent.active, "agent inactive");
        assert!(room.started, "room not started");
        // v6 (audit A1): the revealed amount is bounded at the
        // door. An unbounded amount is not a bigger claim, it is a
        // weapon: the settle accumulators summed raw amounts, so
        // one reveal near the u128 ceiling made every settle of
        // that epoch panic forever and bricked the room with the
        // pool inside. 10^30 yocto (1,000,000 NEAR) is orders above
        // any real room and, with capacity bounded at 1000, keeps
        // every settle sum below 10^33.
        assert!(
            amount.0 <= MAX_REVEAL_YOCTO,
            "amount too large (max 1000000 NEAR)"
        );
        assert!(reasoning_hash.len() <= HASH_STR_MAX, "reasoning_hash too long");
        assert!(log_ref.len() <= LOG_REF_MAX, "log_ref too long");
        assert!(salt.len() <= HASH_STR_MAX, "salt too long");
        let now = env::block_timestamp();
        assert!(now < self.season_end_ns(room), "season over");
        let (epoch, is_commit, _) = self.phase_of(room, now);
        assert!(!is_commit, "not in reveal phase");
        let commitment = self
            .commits
            .get(&(room_id, epoch, caller.clone()))
            .expect("no commit found for this epoch");
        let expected = commitment_for(amount.0, &reasoning_hash, &salt);
        assert_eq!(&expected, commitment, "commitment mismatch");
        assert!(
            self.reveals.get(&(room_id, epoch, caller.clone())).is_none(),
            "already revealed"
        );
        self.reveals.insert(
            (room_id, epoch, caller.clone()),
            RevealRec {
                amount: amount.0,
                reasoning_hash,
                log_ref,
                paid: 0,
            },
        );
        let mut list = self
            .revealers
            .get(&(room_id, epoch))
            .cloned()
            .unwrap_or_default();
        list.push(caller);
        self.revealers.insert((room_id, epoch), list);
    }

    pub fn settle_epoch(&mut self, room_id: u64, epoch: u64) {
        let room = self.rooms.get(&room_id).expect("room not found");
        assert!(room.started, "room not started");
        // v6 (audit A4): epochs at or past the season length do
        // not exist. Settling one used to write a ghost epoch
        // record (a post-season settle of "epoch 12" in a 12-epoch
        // room succeeded and entered the dataset).
        assert!(epoch < room.season_epochs, "epoch out of range for this season");
        assert_eq!(epoch, room.next_epoch_to_settle, "epochs settle in order");
        let now = env::block_timestamp();
        let epoch_end = room.start_ns + (epoch + 1) * room.epoch_secs * NS_PER_SEC;
        assert!(now >= epoch_end, "epoch not finished yet");
        self.settle_one(room_id, epoch);
    }

    /// Settle one epoch whose turn and time have come. Callers
    /// (settle_epoch, finalize_room) perform the gate checks.
    fn settle_one(&mut self, room_id: u64, epoch: u64) {
        let mut room = self.rooms.get(&room_id).expect("room not found").clone();

        // Refill first: this epoch's drip lands before payouts.
        // v4: a Flat room drips refill_per_epoch, as always; a
        // Decaying room drips its current_refill and then decays
        // current_refill for the next epoch, floored at
        // refill_floor. Either way the drip is capped by what the
        // reserve actually holds, so the reserve can never go
        // negative, and Decaying only stretches the reserve, it
        // never mints.
        let drip = match room.refill_mode {
            RefillMode::Flat => room.refill_per_epoch,
            RefillMode::Decaying => room.current_refill,
        };
        let refill_moved = drip.min(room.refill_reserve);
        room.refill_reserve -= refill_moved;
        room.pool_balance += refill_moved;
        if matches!(room.refill_mode, RefillMode::Decaying) {
            // current_refill is bounded by the total NEAR supply
            // and decay_bps by 10000, so the product stays far
            // below the u128 maximum; the checked multiply turns a
            // pathological state into a clear panic instead of a
            // silent wrap.
            let decayed = room
                .current_refill
                .checked_mul(room.decay_bps as u128)
                .expect("refill decay overflow")
                / BPS_DENOMINATOR;
            room.current_refill = decayed.max(room.refill_floor);
        }

        let pool_snapshot = room.pool_balance;
        let cap = pool_snapshot * (room.cap_bps as u128) / BPS_DENOMINATOR;
        // The total payout limit for this epoch. Wild: the whole
        // snapshot, exactly the v1 rule. Paced: the snapshot divided
        // by the epochs remaining in the season (this epoch included),
        // so the final epoch's budget is the entire remaining
        // snapshot. Post-season straggler settles clamp to 1 remaining
        // epoch and behave like a finale.
        let payout_limit = match room.regime {
            Regime::Wild => pool_snapshot,
            Regime::Paced => {
                let epochs_remaining = room.season_epochs.saturating_sub(epoch).max(1);
                pool_snapshot / epochs_remaining as u128
            }
        };
        let revealers = self
            .revealers
            .get(&(room_id, epoch))
            .cloned()
            .unwrap_or_default();

        let mut total_payable: u128 = 0;
        let mut total_requested: u128 = 0;
        let mut payables: Vec<(AccountId, u128)> = Vec::new();
        for who in &revealers {
            if let Some(rec) = self.reveals.get(&(room_id, epoch, who.clone())) {
                // v6 (audit A1): checked accumulation as defense in
                // depth on top of the reveal-side amount bound.
                total_requested = total_requested
                    .checked_add(rec.amount)
                    .expect("total_requested overflow");
                let payable = rec.amount.min(cap);
                total_payable = total_payable
                    .checked_add(payable)
                    .expect("total_payable overflow");
                payables.push((who.clone(), payable));
            }
        }

        let mut total_paid: u128 = 0;
        for (who, payable) in &payables {
            let pay = if total_payable <= payout_limit || total_payable == 0 {
                *payable
            } else {
                mul_div_floor(*payable, payout_limit, total_payable)
            };
            if pay > 0 {
                Promise::new(who.clone()).transfer(NearToken::from_yoctonear(pay));
            }
            total_paid += pay;
            if let Some(mut rec) = self.reveals.get(&(room_id, epoch, who.clone())).cloned() {
                rec.paid = pay;
                self.reveals.insert((room_id, epoch, who.clone()), rec);
            }
            if let Some(mut agent) = self.agents.get(&(room_id, who.clone())).cloned() {
                agent.total_withdrawn += pay;
                // v3: this reveal is now settled, so it counts once
                // toward the agent's epochs_revealed (the finalize
                // top-up is priced per settled reveal).
                agent.epochs_revealed += 1;
                self.agents.insert((room_id, who.clone()), agent);
            }
        }

        room.pool_balance -= total_paid;
        room.total_paid_out += total_paid;
        room.next_epoch_to_settle += 1;
        let pool_after = room.pool_balance;
        self.rooms.insert(room_id, room);
        self.epochs.insert(
            (room_id, epoch),
            EpochRec {
                epoch,
                total_requested,
                total_paid,
                refill_moved,
                pool_after,
                reveal_count: revealers.len() as u32,
            },
        );
        env::log_str(&format!(
            "COMMONS epoch_settled room={} epoch={} paid={}",
            room_id, epoch, total_paid
        ));
    }

    // ---------- exit and season end ----------

    /// Leave the room and take the stake back (minus the exit
    /// tax mid-season). EXIT IS PERMANENT (audit A4, documented):
    /// the account's record stays in the room, registration
    /// rejects a second seat, and there is no rejoin, in this or
    /// any earlier version. An operator who exits gives up the
    /// seat for the rest of the season.
    pub fn exit_room(&mut self, room_id: u64) {
        let caller = env::predecessor_account_id();
        let mut room = self.rooms.get(&room_id).expect("room not found").clone();
        let mut agent = self
            .agents
            .get(&(room_id, caller.clone()))
            .expect("not registered in this room")
            .clone();
        assert!(agent.active, "already inactive");
        agent.active = false;
        // A room that has not started has no season running, so an
        // exit during recruiting is an early exit under the v1 rule
        // (with a zero stake the tax is zero and nothing moves).
        // v4 exception: in a CANCELLED room nothing was ever
        // played, so the stake comes back in full with no tax at
        // all; cancel_room promises exactly that reclaim.
        let season_over = self.season_over(&room, env::block_timestamp());
        let tax = if season_over || room.ended || room.cancelled {
            0
        } else {
            agent.stake * EXIT_TAX_BPS / BPS_DENOMINATOR
        };
        room.security_pot += tax;
        match agent.role {
            Role::Feeder => room.feeders_count = room.feeders_count.saturating_sub(1),
            Role::Security => room.security_count = room.security_count.saturating_sub(1),
        }
        let refund = agent.stake - tax;
        agent.stake = 0;
        self.agents.insert((room_id, caller.clone()), agent);
        self.rooms.insert(room_id, room);
        if refund > 0 {
            Promise::new(caller).transfer(NearToken::from_yoctonear(refund));
        }
    }

    pub fn finalize_room(&mut self, room_id: u64) {
        let mut room = self.rooms.get(&room_id).expect("room not found").clone();
        assert!(!room.ended, "already finalized");
        assert!(room.started, "room not started");
        assert!(
            env::block_timestamp() >= self.season_end_ns(&room),
            "season not over yet"
        );
        // v6 (audit A2, adopting the outside reviewer's proposal):
        // finalize settles every remaining epoch itself, in order,
        // before distributing anything. v5 required the caller to
        // have settled all epochs first without enforcing it, so a
        // room could finalize with epochs unsettled and their
        // payouts simply never made. Here the invariant
        // next_epoch_to_settle == season_epochs holds by
        // construction at the distribution point; the assert below
        // states it.
        drop(room);
        loop {
            let r = self.rooms.get(&room_id).expect("room not found");
            if r.next_epoch_to_settle >= r.season_epochs {
                break;
            }
            let next = r.next_epoch_to_settle;
            self.settle_one(room_id, next);
        }
        let mut room = self.rooms.get(&room_id).expect("room not found").clone();
        assert_eq!(
            room.next_epoch_to_settle, room.season_epochs,
            "finalize invariant: all epochs settled"
        );
        room.ended = true;
        // v3 finalize distribution order: (a) security retainers,
        // (b) feeder top-ups, (c) the security pot split below
        // (unchanged from v1 and v2). Unspent GF is not touched
        // here; it stays in gf_remaining and returns to the sponsor
        // inside withdraw_residue.
        let roster = self
            .rosters
            .get(&room_id)
            .cloned()
            .unwrap_or_default();
        // (a) Retainers: every still-active security agent receives
        // retainer_per_security out of the room's retainer reserve,
        // first come in registration order, capped by what the
        // reserve holds. Whatever is left after paying them goes to
        // the owner, so the reserve always ends fully distributed
        // and is zeroed on the room.
        let mut retainer_left = room.retainer_reserve;
        if retainer_left > 0 {
            for who in &roster {
                if retainer_left == 0 {
                    break;
                }
                if let Some(agent) = self.agents.get(&(room_id, who.clone())) {
                    if agent.active && matches!(agent.role, Role::Security) {
                        let pay = self.retainer_per_security.min(retainer_left);
                        if pay > 0 {
                            Promise::new(who.clone())
                                .transfer(NearToken::from_yoctonear(pay));
                            retainer_left -= pay;
                        }
                    }
                }
            }
            if retainer_left > 0 {
                Promise::new(self.owner.clone())
                    .transfer(NearToken::from_yoctonear(retainer_left));
            }
            room.retainer_reserve = 0;
        }
        // (b) Feeder top-ups, Covered rooms only: every
        // still-active feeder who settled at least one reveal is
        // brought up to epochs_revealed x gas_per_epoch if their
        // pool payouts fell short of that, paid from the Guarantee
        // Fund. v4: the target is gas only. The v3 flat topup_eps
        // is deliberately NOT added to the target: it was a profit
        // sweetener on top of gas, not loss protection, and it was
        // the term the fund did not cover in short seasons. The
        // (v6: the stored topup_eps config value is deleted
        // outright.) A Bare room has
        // no fund and pays no top-ups: its gf_remaining is 0 and
        // the coverage check skips this step entirely. A feeder who
        // never revealed spent no epoch gas and gets nothing; a
        // feeder who exited early or was slashed is inactive and
        // gets nothing (the guarantee is void by design). Top-ups
        // do not count into total_withdrawn, which tracks pool
        // payouts only.
        if matches!(room.coverage, Coverage::Covered) && room.gf_remaining > 0 {
            for who in &roster {
                if room.gf_remaining == 0 {
                    break;
                }
                if let Some(agent) = self.agents.get(&(room_id, who.clone())) {
                    if agent.active
                        && matches!(agent.role, Role::Feeder)
                        && agent.epochs_revealed > 0
                    {
                        let target =
                            (agent.epochs_revealed as u128) * self.gas_per_epoch;
                        let due = target.saturating_sub(agent.total_withdrawn);
                        let pay = due.min(room.gf_remaining);
                        if pay > 0 {
                            Promise::new(who.clone())
                                .transfer(NearToken::from_yoctonear(pay));
                            room.gf_remaining -= pay;
                        }
                    }
                }
            }
        }
        if !room.pot_distributed && room.security_pot > 0 {
            // Qualifiers: still-active security agents with at least one
            // confirmed report authored in this room.
            let mut qualifiers: Vec<AccountId> = Vec::new();
            for r in self.reports.values() {
                if r.room_id == room_id && r.confirmed {
                    if let Some(rep) = self.agents.get(&(room_id, r.reporter.clone())) {
                        if rep.active
                            && matches!(rep.role, Role::Security)
                            && !qualifiers.contains(&r.reporter)
                        {
                            qualifiers.push(r.reporter.clone());
                        }
                    }
                }
            }
            if qualifiers.is_empty() {
                room.pool_balance += room.security_pot;
                room.security_pot = 0;
            } else {
                let share = room.security_pot / qualifiers.len() as u128;
                for q in &qualifiers {
                    if share > 0 {
                        Promise::new(q.clone()).transfer(NearToken::from_yoctonear(share));
                    }
                }
                room.security_pot -= share * qualifiers.len() as u128;
                // Any dust stays in the pot field and is part of residue.
                room.pool_balance += room.security_pot;
                room.security_pot = 0;
            }
            room.pot_distributed = true;
        }
        self.rooms.insert(room_id, room);
        env::log_str(&format!("COMMONS room_finalized id={}", room_id));
    }

    pub fn withdraw_residue(&mut self, room_id: u64) {
        let caller = env::predecessor_account_id();
        let mut room = self.rooms.get(&room_id).expect("room not found").clone();
        assert_eq!(caller, room.sponsor, "only the sponsor");
        assert!(room.ended, "room not finalized");
        assert!(!room.residue_withdrawn, "residue already withdrawn");
        // v3: residue is pool + reserve + whatever the Guarantee
        // Fund did not spend on top-ups.
        let amount = room.pool_balance + room.refill_reserve + room.gf_remaining;
        room.pool_balance = 0;
        room.refill_reserve = 0;
        room.gf_remaining = 0;
        room.residue_withdrawn = true;
        self.rooms.insert(room_id, room);
        if amount > 0 {
            Promise::new(caller).transfer(NearToken::from_yoctonear(amount));
        }
    }

    // ---------- security ----------

    pub fn report_abuse(
        &mut self,
        room_id: u64,
        target: AccountId,
        evidence_hash: String,
    ) -> u64 {
        let caller = env::predecessor_account_id();
        let reporter = self
            .agents
            .get(&(room_id, caller.clone()))
            .expect("reporter not registered in this room");
        assert!(reporter.active, "reporter inactive");
        assert!(
            matches!(reporter.role, Role::Security),
            "only security agents report"
        );
        let target_rec = self
            .agents
            .get(&(room_id, target.clone()))
            .expect("target not registered in this room");
        assert!(target_rec.active, "target inactive");
        assert!(
            matches!(target_rec.role, Role::Feeder),
            "target must be a feeder"
        );
        // v6 (audit A3): reports live and die with the season. A
        // report filed after the room ended could still be
        // confirmed, and its slash would land in a security pot
        // whose distribution already ran, stranding the funds.
        let room = self.rooms.get(&room_id).expect("room not found");
        assert!(room.started, "room not started");
        assert!(!room.ended, "room ended");
        assert!(!room.cancelled, "room cancelled");
        assert!(
            evidence_hash.len() <= HASH_STR_MAX,
            "evidence_hash too long"
        );
        // The threshold is snapshotted from the security set as it
        // stands NOW (see the Report::threshold note).
        let threshold = room.security_count / 2 + 1;
        let id = self.next_report_id;
        self.next_report_id += 1;
        self.reports.insert(
            id,
            Report {
                id,
                room_id,
                reporter: caller,
                target,
                evidence_hash,
                confirms: Vec::new(),
                confirmed: false,
                threshold,
            },
        );
        id
    }

    pub fn confirm_report(&mut self, report_id: u64) {
        let caller = env::predecessor_account_id();
        let mut report = self
            .reports
            .get(&report_id)
            .expect("report not found")
            .clone();
        assert!(!report.confirmed, "already confirmed");
        let room = self
            .rooms
            .get(&report.room_id)
            .expect("room not found")
            .clone();
        let confirmer = self
            .agents
            .get(&(report.room_id, caller.clone()))
            .expect("confirmer not registered in this room");
        assert!(confirmer.active, "confirmer inactive");
        assert!(
            matches!(confirmer.role, Role::Security),
            "only security agents confirm"
        );
        assert!(caller != report.reporter, "reporter cannot confirm own report");
        assert!(caller != report.target, "target cannot confirm");
        assert!(
            !report.confirms.contains(&caller),
            "already confirmed by caller"
        );
        // v6 (audit A3): no confirming into a finished room; the
        // slash would strand in an already-distributed pot.
        assert!(!room.ended, "room ended");
        assert!(!room.cancelled, "room cancelled");
        report.confirms.push(caller);
        if report.confirms.len() >= report.threshold as usize {
            report.confirmed = true;
            // Slash the target: part of the stake goes to the security
            // pot, the rest is refunded, the feeder is deactivated.
            let mut target = self
                .agents
                .get(&(report.room_id, report.target.clone()))
                .expect("target missing")
                .clone();
            let mut slashed = false;
            if target.active {
                slashed = true;
                target.active = false;
                let slash = target.stake * ABUSE_SLASH_BPS / BPS_DENOMINATOR;
                let refund = target.stake - slash;
                target.stake = 0;
                self.agents
                    .insert((report.room_id, report.target.clone()), target);
                let mut room2 = room.clone();
                room2.security_pot += slash;
                room2.feeders_count = room2.feeders_count.saturating_sub(1);
                self.rooms.insert(report.room_id, room2);
                if refund > 0 {
                    Promise::new(report.target.clone())
                        .transfer(NearToken::from_yoctonear(refund));
                }
            }
            // v6 (audit A3): the reporter is credited only when a
            // slash actually happened. Crediting a confirmation
            // whose target had already exited (no slash, no pot)
            // still qualified the reporter for the pot split at
            // finalize, which is credit for nothing.
            if slashed {
                if let Some(mut rep) = self
                    .agents
                    .get(&(report.room_id, report.reporter.clone()))
                    .cloned()
                {
                    rep.confirmed_reports_authored += 1;
                    self.agents
                        .insert((report.room_id, report.reporter.clone()), rep);
                }
            }
            env::log_str(&format!(
                "COMMONS abuse_confirmed report={} room={}",
                report_id, report.room_id
            ));
        }
        self.reports.insert(report_id, report);
    }

    // ---------- profiles ----------

    /// Store or overwrite the caller's display profile. Display
    /// metadata only: profiles are read by interfaces and touch no
    /// room, stake, or payout logic.
    pub fn set_profile(&mut self, display_name: String, bio: String, social: String) {
        assert!(
            display_name.chars().count() <= PROFILE_NAME_MAX,
            "display_name too long (max 64 chars)"
        );
        assert!(
            bio.chars().count() <= PROFILE_BIO_MAX,
            "bio too long (max 160 chars)"
        );
        assert!(
            social.chars().count() <= PROFILE_SOCIAL_MAX,
            "social too long (max 64 chars)"
        );
        let caller = env::predecessor_account_id();
        self.profiles.insert(
            caller,
            Profile {
                display_name,
                bio,
                social,
            },
        );
    }

    // ---------- views ----------

    pub fn get_config(&self) -> ConfigView {
        ConfigView {
            owner: self.owner.to_string(),
            room_fee: self.room_fee.to_string(),
            exit_tax_bps: EXIT_TAX_BPS as u32,
            abuse_slash_bps: ABUSE_SLASH_BPS as u32,
            rooms_created: self.next_room_id,
            gas_per_epoch: self.gas_per_epoch.to_string(),
            retainer_per_security: self.retainer_per_security.to_string(),
        }
    }

    pub fn get_room(&self, room_id: u64) -> Option<RoomView> {
        self.rooms.get(&room_id).map(room_view)
    }

    pub fn list_rooms(&self, from_index: u64, limit: u64) -> Vec<RoomView> {
        self.rooms
            .iter()
            .skip(from_index as usize)
            .take(limit as usize)
            .map(|(_, r)| room_view(r))
            .collect()
    }

    pub fn get_agent(&self, room_id: u64, account: AccountId) -> Option<AgentView> {
        self.agents.get(&(room_id, account.clone())).map(|a| AgentView {
            account: account.to_string(),
            role: role_str(a.role).to_string(),
            stake: a.stake.to_string(),
            active: a.active,
            total_withdrawn: a.total_withdrawn.to_string(),
            confirmed_reports_authored: a.confirmed_reports_authored,
            epochs_revealed: a.epochs_revealed,
        })
    }

    pub fn get_epoch(&self, room_id: u64, epoch: u64) -> Option<EpochView> {
        let rec = self.epochs.get(&(room_id, epoch))?;
        let revealers = self
            .revealers
            .get(&(room_id, epoch))
            .cloned()
            .unwrap_or_default();
        let mut reveals = Vec::new();
        for who in revealers {
            if let Some(r) = self.reveals.get(&(room_id, epoch, who.clone())) {
                reveals.push(RevealView {
                    account: who.to_string(),
                    amount: r.amount.to_string(),
                    reasoning_hash: r.reasoning_hash.clone(),
                    log_ref: r.log_ref.clone(),
                    paid: r.paid.to_string(),
                });
            }
        }
        Some(EpochView {
            room_id,
            epoch: rec.epoch,
            total_requested: rec.total_requested.to_string(),
            total_paid: rec.total_paid.to_string(),
            refill_moved: rec.refill_moved.to_string(),
            pool_after: rec.pool_after.to_string(),
            reveal_count: rec.reveal_count,
            reveals,
        })
    }

    pub fn get_report(&self, report_id: u64) -> Option<ReportView> {
        self.reports.get(&report_id).map(|r| ReportView {
            id: r.id,
            room_id: r.room_id,
            reporter: r.reporter.to_string(),
            target: r.target.to_string(),
            evidence_hash: r.evidence_hash.clone(),
            confirms: r.confirms.len() as u32,
            confirmed: r.confirmed,
        })
    }

    pub fn get_profile(&self, account: AccountId) -> Option<ProfileView> {
        self.profiles.get(&account).map(|p| ProfileView {
            account: account.to_string(),
            display_name: p.display_name.clone(),
            bio: p.bio.clone(),
            social: p.social.clone(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use near_sdk::test_utils::VMContextBuilder;
    use near_sdk::testing_env;

    const T0: u64 = 1_000_000_000_000; // ns
    const SEC: u64 = NS_PER_SEC;

    fn acct(s: &str) -> AccountId {
        s.parse().unwrap()
    }
    fn owner() -> AccountId {
        acct("owner.testnet")
    }
    fn sponsor() -> AccountId {
        acct("sponsor.testnet")
    }
    fn sec(i: u32) -> AccountId {
        acct(&format!("sec{}.testnet", i))
    }
    fn alice() -> AccountId {
        acct("alice.testnet")
    }
    fn bob() -> AccountId {
        acct("bob.testnet")
    }

    fn ctx(account: &AccountId, deposit: u128, ts: u64) -> VMContextBuilder {
        let mut builder = VMContextBuilder::new();
        builder
            .predecessor_account_id(account.clone())
            .attached_deposit(NearToken::from_yoctonear(deposit))
            .block_timestamp(ts);
        builder
    }

    fn setup() -> Commons {
        testing_env!(ctx(&owner(), 0, T0).build());
        Commons::new(owner(), U128(10))
    }

    fn create(
        c: &mut Commons,
        regime: Regime,
        stake: u128,
        cap_bps: u32,
        refill: u128,
        seed: u128,
        season_epochs: u64,
    ) -> u64 {
        // v4 default room: Covered (so the GF deposit math matches
        // the v3 tests), Flat refill.
        create2(
            c,
            regime,
            stake,
            cap_bps,
            refill,
            seed,
            season_epochs,
            Coverage::Covered,
            RefillMode::Flat,
            DEFAULT_DECAY_BPS,
            0,
        )
    }

    /// Full-knob create on the small-fee setup (fee 10, capacity
    /// 10, epoch 100 s). The deposit is computed from the coverage:
    /// Bare rooms deposit fee + seed only.
    #[allow(clippy::too_many_arguments)]
    fn create2(
        c: &mut Commons,
        regime: Regime,
        stake: u128,
        cap_bps: u32,
        refill: u128,
        seed: u128,
        season_epochs: u64,
        coverage: Coverage,
        refill_mode: RefillMode,
        decay_bps: u32,
        refill_floor: u128,
    ) -> u64 {
        let gf = match coverage {
            Coverage::Covered => guarantee_fund_for(10, season_epochs, GAS_PER_EPOCH),
            Coverage::Bare => 0,
        };
        testing_env!(ctx(&sponsor(), 10 + seed + gf, T0).build());
        c.create_room(
            "test".to_string(),
            regime,
            U128(stake),
            10,
            U128(seed),
            U128(refill),
            cap_bps,
            100,
            season_epochs,
            coverage,
            refill_mode,
            decay_bps,
            U128(refill_floor),
        )
    }

    fn register(c: &mut Commons, room: u64, who: &AccountId, security: bool, stake: u128) {
        testing_env!(ctx(who, stake, T0).build());
        if security {
            c.register_security(room);
        } else {
            c.register_agent(room);
        }
    }

    fn start(c: &mut Commons, room: u64) {
        testing_env!(ctx(&sponsor(), 0, T0).build());
        c.start_room(room);
    }

    fn seat_security(c: &mut Commons, room: u64, stake: u128) {
        for i in 1..=3 {
            register(c, room, &sec(i), true, stake);
        }
    }

    fn seat_security_and_start(c: &mut Commons, room: u64, stake: u128) {
        seat_security(c, room, stake);
        // v5: the start quorum also needs MIN_FEEDERS_TO_START
        // feeders seated.
        for i in 1..=MIN_FEEDERS_TO_START {
            register(c, room, &feeder(i), false, stake);
        }
        start(c, room);
    }

    /// v1-compatible default room: Wild regime, stake 1000, 5 epochs of
    /// 100 s, already started at T0 with 3 security seated.
    fn make_room(c: &mut Commons, cap_bps: u32, refill: u128, seed: u128) -> u64 {
        let id = create(c, Regime::Wild, 1_000, cap_bps, refill, seed, 5);
        seat_security_and_start(c, id, 1_000);
        id
    }

    // ---------- ported v1 tests ----------

    #[test]
    fn room_accounting() {
        let mut c = setup();
        let room_id = make_room(&mut c, 500, 100, 10_000);
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.pool_balance, "2000"); // 20% of seed
        assert_eq!(room.refill_reserve, "8000");
        assert_eq!(room.stake, "1000");
        assert_eq!(room.regime, "wild");
        assert!(room.started);
    }

    #[test]
    fn full_epoch_with_cap() {
        let mut c = setup();
        let room_id = make_room(&mut c, 500, 100, 10_000);
        let a = alice();
        let b = bob();
        register(&mut c, room_id, &a, false, 1_000);
        register(&mut c, room_id, &b, false, 1_000);

        // Commit phase: first half of epoch 0 (epoch = 100 s).
        let hash_a = commitment_for(50, "ra", "sa");
        let hash_b = commitment_for(500, "rb", "sb");
        testing_env!(ctx(&a, 0, T0 + 10 * SEC).build());
        c.commit(room_id, hash_a);
        testing_env!(ctx(&b, 0, T0 + 10 * SEC).build());
        c.commit(room_id, hash_b);

        // Reveal phase: second half.
        testing_env!(ctx(&a, 0, T0 + 60 * SEC).build());
        c.reveal(room_id, U128(50), "ra".into(), "log-a".into(), "sa".into());
        testing_env!(ctx(&b, 0, T0 + 60 * SEC).build());
        c.reveal(room_id, U128(500), "rb".into(), "log-b".into(), "sb".into());

        // Settle after the epoch ends. Pool snapshot = 2000 + 100 refill
        // = 2100, cap = 5% = 105. A asked 50 (paid 50), B asked 500
        // (capped to 105). Pool after = 2100 - 155 = 1945.
        testing_env!(ctx(&a, 0, T0 + 101 * SEC).build());
        c.settle_epoch(room_id, 0);
        let view = c.get_epoch(room_id, 0).unwrap();
        assert_eq!(view.total_paid, "155");
        assert_eq!(view.pool_after, "1945");
        assert_eq!(c.get_agent(room_id, a).unwrap().total_withdrawn, "50");
        assert_eq!(c.get_agent(room_id, b).unwrap().total_withdrawn, "105");
    }

    #[test]
    fn prorata_when_pool_short() {
        let mut c = setup();
        // Seed 1000 -> pool 200, no refill, cap 100% of pool.
        let room_id = make_room(&mut c, 10_000, 0, 1_000);
        let a = alice();
        let b = bob();
        register(&mut c, room_id, &a, false, 1_000);
        register(&mut c, room_id, &b, false, 1_000);
        testing_env!(ctx(&a, 0, T0 + 10 * SEC).build());
        c.commit(room_id, commitment_for(150, "ra", "sa"));
        testing_env!(ctx(&b, 0, T0 + 10 * SEC).build());
        c.commit(room_id, commitment_for(150, "rb", "sb"));
        testing_env!(ctx(&a, 0, T0 + 60 * SEC).build());
        c.reveal(room_id, U128(150), "ra".into(), "".into(), "sa".into());
        testing_env!(ctx(&b, 0, T0 + 60 * SEC).build());
        c.reveal(room_id, U128(150), "rb".into(), "".into(), "sb".into());
        testing_env!(ctx(&a, 0, T0 + 101 * SEC).build());
        c.settle_epoch(room_id, 0);
        // Demand 300 against a pool of 200: each gets 150 * 200 / 300 = 100.
        assert_eq!(c.get_agent(room_id, a).unwrap().total_withdrawn, "100");
        assert_eq!(c.get_agent(room_id, b).unwrap().total_withdrawn, "100");
        assert_eq!(c.get_room(room_id).unwrap().pool_balance, "0");
    }

    #[test]
    fn early_exit_tax() {
        let mut c = setup();
        let room_id = make_room(&mut c, 500, 100, 10_000);
        let a = alice();
        register(&mut c, room_id, &a, false, 1_000);
        testing_env!(ctx(&a, 0, T0 + 2 * SEC).build());
        c.exit_room(room_id);
        let agent = c.get_agent(room_id, a).unwrap();
        assert!(!agent.active);
        // 10% of the 1000 stake goes to the security pot.
        assert_eq!(c.get_room(room_id).unwrap().security_pot, "100");
    }

    #[test]
    fn abuse_report_slashes() {
        let mut c = setup();
        let room_id = make_room(&mut c, 500, 100, 10_000);
        let feeder = alice();
        register(&mut c, room_id, &feeder, false, 1_000);
        // make_room seated sec1..sec3 before starting the room.
        // Threshold: security_count 3 -> floor(3/2)+1 = 2 confirms, and
        // the reporter cannot confirm their own report.
        testing_env!(ctx(&sec(1), 0, T0 + 2 * SEC).build());
        let report_id = c.report_abuse(room_id, feeder.clone(), "evidence".into());
        testing_env!(ctx(&sec(2), 0, T0 + 3 * SEC).build());
        c.confirm_report(report_id);
        assert!(!c.get_report(report_id).unwrap().confirmed);
        testing_env!(ctx(&sec(3), 0, T0 + 3 * SEC).build());
        c.confirm_report(report_id);
        let report = c.get_report(report_id).unwrap();
        assert!(report.confirmed);
        let agent = c.get_agent(room_id, feeder).unwrap();
        assert!(!agent.active);
        // 20% of 1000 slashed into the pot.
        assert_eq!(c.get_room(room_id).unwrap().security_pot, "200");
    }

    // ---------- v2 tests ----------

    #[test]
    fn paced_linear_decline_under_max_greed() {
        let mut c = setup();
        // Pool 2000, no refill, cap 100% (per-agent cap never binds
        // below the budget), 4 epochs. Two feeders demand effectively
        // infinite amounts every epoch.
        let room_id = create(&mut c, Regime::Paced, 1_000, 10_000, 0, 10_000, 4);
        seat_security_and_start(&mut c, room_id, 1_000);
        let feeders = [alice(), bob()];
        for f in &feeders {
            register(&mut c, room_id, f, false, 1_000);
        }
        let expected_pool_after = ["1500", "1000", "500", "0"];
        for epoch in 0..4u64 {
            let base = T0 + epoch * 100 * SEC;
            for (i, who) in feeders.iter().enumerate() {
                let hash = commitment_for(1_000_000, &format!("r{i}"), &format!("s{i}"));
                testing_env!(ctx(who, 0, base + 10 * SEC).build());
                c.commit(room_id, hash);
            }
            for (i, who) in feeders.iter().enumerate() {
                testing_env!(ctx(who, 0, base + 60 * SEC).build());
                c.reveal(
                    room_id,
                    U128(1_000_000),
                    format!("r{i}"),
                    "".into(),
                    format!("s{i}"),
                );
            }
            testing_env!(ctx(&sponsor(), 0, base + 101 * SEC).build());
            c.settle_epoch(room_id, epoch);
            let view = c.get_epoch(room_id, epoch).unwrap();
            // Budget each epoch: snapshot / epochs remaining =
            // 2000/4, 1500/3, 1000/2, 500/1 = 500 every time.
            assert_eq!(view.total_paid, "500", "epoch {}", epoch);
            assert_eq!(view.pool_after, expected_pool_after[epoch as usize]);
        }
        // The pool is exhausted exactly at the final epoch, and the
        // finale paid the entire remainder out to the feeders.
        assert_eq!(c.get_room(room_id).unwrap().pool_balance, "0");
        assert_eq!(c.get_room(room_id).unwrap().total_paid_out, "2000");
        for f in &feeders {
            assert_eq!(c.get_agent(room_id, f.clone()).unwrap().total_withdrawn, "1000");
        }
    }

    #[test]
    #[should_panic(expected = "room not started")]
    fn commit_before_start_rejected() {
        let mut c = setup();
        let room_id = create(&mut c, Regime::Wild, 1_000, 500, 100, 10_000, 5);
        let a = alice();
        // Registration during recruiting works; playing does not.
        register(&mut c, room_id, &a, false, 1_000);
        testing_env!(ctx(&a, 0, T0 + 10 * SEC).build());
        c.commit(room_id, commitment_for(50, "ra", "sa"));
    }

    #[test]
    #[should_panic(expected = "room not started")]
    fn settle_before_start_rejected() {
        let mut c = setup();
        let room_id = create(&mut c, Regime::Wild, 1_000, 500, 100, 10_000, 5);
        testing_env!(ctx(&sponsor(), 0, T0 + 10_000 * SEC).build());
        c.settle_epoch(room_id, 0);
    }

    #[test]
    #[should_panic(expected = "have 2")]
    fn start_gate_rejects_two_security() {
        let mut c = setup();
        let room_id = create(&mut c, Regime::Wild, 1_000, 500, 100, 10_000, 5);
        register(&mut c, room_id, &sec(1), true, 1_000);
        register(&mut c, room_id, &sec(2), true, 1_000);
        start(&mut c, room_id);
    }

    #[test]
    fn start_room_permissionless_once_gate_met() {
        // v4 replaces the v3 test start_gate_rejects_non_sponsor:
        // the sponsor-only start rule is gone. A complete stranger
        // starts the room once the quorum is seated (3 security,
        // and since v5 also MIN_FEEDERS_TO_START feeders).
        let mut c = setup();
        let room_id = create(&mut c, Regime::Wild, 1_000, 500, 100, 10_000, 5);
        for i in 1..=3 {
            register(&mut c, room_id, &sec(i), true, 1_000);
        }
        for i in 1..=MIN_FEEDERS_TO_START {
            register(&mut c, room_id, &feeder(i), false, 1_000);
        }
        testing_env!(ctx(&acct("stranger.testnet"), 0, T0).build());
        c.start_room(room_id);
        let room = c.get_room(room_id).unwrap();
        assert!(room.started);
        assert_eq!(room.start_ns, T0.to_string());
    }

    #[test]
    #[should_panic(expected = "feeder agents to start, have 1")]
    fn start_gate_rejects_one_feeder() {
        // v5: a full security bench cannot start a room that is
        // still waiting for players. Even the sponsor is rejected
        // below the feeder quorum.
        let mut c = setup();
        let room_id = create(&mut c, Regime::Wild, 1_000, 500, 100, 10_000, 5);
        for i in 1..=3 {
            register(&mut c, room_id, &sec(i), true, 1_000);
        }
        register(&mut c, room_id, &feeder(1), false, 1_000);
        start(&mut c, room_id);
    }

    #[test]
    fn start_opens_at_quorum_called_by_joining_feeder() {
        // v5 auto-start shape: the feeder whose registration
        // completes the quorum fires start_room itself, no sponsor
        // involved, and the season clock opens.
        let mut c = setup();
        let room_id = create(&mut c, Regime::Wild, 1_000, 500, 100, 10_000, 5);
        for i in 1..=3 {
            register(&mut c, room_id, &sec(i), true, 1_000);
        }
        register(&mut c, room_id, &feeder(1), false, 1_000);
        register(&mut c, room_id, &feeder(2), false, 1_000);
        testing_env!(ctx(&feeder(2), 0, T0).build());
        c.start_room(room_id);
        let room = c.get_room(room_id).unwrap();
        assert!(room.started);
        assert_eq!(room.start_ns, T0.to_string());
    }

    #[test]
    #[should_panic(expected = "security agents to start")]
    fn start_room_by_stranger_before_gate_fails() {
        // Permissionless does not mean gateless: with only 2
        // security seated, even a stranger's start is rejected by
        // the same MIN_SECURITY_TO_START gate.
        let mut c = setup();
        let room_id = create(&mut c, Regime::Wild, 1_000, 500, 100, 10_000, 5);
        register(&mut c, room_id, &sec(1), true, 1_000);
        register(&mut c, room_id, &sec(2), true, 1_000);
        testing_env!(ctx(&alice(), 0, T0).build());
        c.start_room(room_id);
    }

    #[test]
    fn start_accepted_with_three_security() {
        let mut c = setup();
        let room_id = create(&mut c, Regime::Paced, 1_000, 500, 100, 10_000, 5);
        // Before start: recruiting state, no clock.
        let info = c.current_epoch(room_id);
        assert_eq!(info.phase, "recruiting");
        assert!(!info.started);
        assert!(!info.season_over);
        assert_eq!(c.get_room(room_id).unwrap().regime, "paced");
        for i in 1..=3 {
            register(&mut c, room_id, &sec(i), true, 1_000);
        }
        for i in 1..=MIN_FEEDERS_TO_START {
            register(&mut c, room_id, &feeder(i), false, 1_000);
        }
        start(&mut c, room_id);
        let room = c.get_room(room_id).unwrap();
        assert!(room.started);
        assert_eq!(room.start_ns, T0.to_string());
        let info = c.current_epoch(room_id);
        assert!(info.started);
        assert_eq!(info.phase, "commit");
        assert_eq!(info.epoch, 0);
    }

    #[test]
    fn stake_zero_full_cycle() {
        let mut c = setup();
        let room_id = create(&mut c, Regime::Wild, 0, 500, 100, 10_000, 5);
        // Everyone registers with a 0 deposit: gas is the only cost.
        seat_security_and_start(&mut c, room_id, 0);
        let a = alice();
        register(&mut c, room_id, &a, false, 0);
        // Play epoch 0: snapshot 2100, cap 105, request 50, paid 50.
        testing_env!(ctx(&a, 0, T0 + 10 * SEC).build());
        c.commit(room_id, commitment_for(50, "ra", "sa"));
        testing_env!(ctx(&a, 0, T0 + 60 * SEC).build());
        c.reveal(room_id, U128(50), "ra".into(), "".into(), "sa".into());
        testing_env!(ctx(&a, 0, T0 + 101 * SEC).build());
        c.settle_epoch(room_id, 0);
        assert_eq!(c.get_agent(room_id, a.clone()).unwrap().total_withdrawn, "50");
        // Exit mid-season: 10% tax of a zero stake is zero, no refund,
        // no panic, and the security pot stays empty.
        testing_env!(ctx(&a, 0, T0 + 150 * SEC).build());
        c.exit_room(room_id);
        let agent = c.get_agent(room_id, a).unwrap();
        assert!(!agent.active);
        assert_eq!(agent.stake, "0");
        assert_eq!(c.get_room(room_id).unwrap().security_pot, "0");
    }

    #[test]
    fn profile_set_get_overwrite() {
        let mut c = setup();
        let a = alice();
        assert!(c.get_profile(a.clone()).is_none());
        testing_env!(ctx(&a, 0, T0).build());
        c.set_profile("Alice".into(), "feeds the commons".into(), "@alice".into());
        let p = c.get_profile(a.clone()).unwrap();
        assert_eq!(p.account, a.to_string());
        assert_eq!(p.display_name, "Alice");
        assert_eq!(p.bio, "feeds the commons");
        assert_eq!(p.social, "@alice");
        // Overwrite replaces all three fields.
        testing_env!(ctx(&a, 0, T0 + SEC).build());
        c.set_profile("Alice A.".into(), "new bio".into(), "@alice2".into());
        let p = c.get_profile(a.clone()).unwrap();
        assert_eq!(p.display_name, "Alice A.");
        assert_eq!(p.bio, "new bio");
        assert_eq!(p.social, "@alice2");
        // Another account is untouched.
        assert!(c.get_profile(bob()).is_none());
    }

    fn play_two(c: &mut Commons, room: u64, epoch: u64, amt_a: u128, amt_b: u128) {
        let base = T0 + epoch * 100 * SEC;
        let a = alice();
        let b = bob();
        testing_env!(ctx(&a, 0, base + 10 * SEC).build());
        c.commit(room, commitment_for(amt_a, "ra", "sa"));
        testing_env!(ctx(&b, 0, base + 10 * SEC).build());
        c.commit(room, commitment_for(amt_b, "rb", "sb"));
        testing_env!(ctx(&a, 0, base + 60 * SEC).build());
        c.reveal(room, U128(amt_a), "ra".into(), "".into(), "sa".into());
        testing_env!(ctx(&b, 0, base + 60 * SEC).build());
        c.reveal(room, U128(amt_b), "rb".into(), "".into(), "sb".into());
        testing_env!(ctx(&sponsor(), 0, base + 101 * SEC).build());
        c.settle_epoch(room, epoch);
    }

    #[test]
    fn prorata_at_yocto_scale() {
        // Regression: the pro-rata product payable * limit is about
        // 1e44 at real yocto scale and overflowed a plain u128
        // multiply (caught live on the first v2 deployment).
        let mut c = setup();
        // Seed 8e22 -> pool 1.6e22, no refill, cap 100% of snapshot.
        let room_id = make_room(&mut c, 10_000, 0, 80_000_000_000_000_000_000_000);
        let a = alice();
        let b = bob();
        register(&mut c, room_id, &a, false, 1_000);
        register(&mut c, room_id, &b, false, 1_000);
        let snap = 16_000_000_000_000_000_000_000u128;
        play_two(&mut c, room_id, 0, snap, snap);
        // Each capped demand is the whole snapshot; total demand is
        // twice the pool, so each feeder gets exactly half the pool.
        let half = 8_000_000_000_000_000_000_000u128;
        assert_eq!(c.get_agent(room_id, a).unwrap().total_withdrawn, half.to_string());
        assert_eq!(c.get_agent(room_id, b).unwrap().total_withdrawn, half.to_string());
        assert_eq!(c.get_room(room_id).unwrap().pool_balance, "0");
    }

    #[test]
    fn paced_budget_at_yocto_scale() {
        // Same numbers as live room A on the v2 deployment: seed
        // 6e22 (pool 1.2e22, reserve 4.8e22), refill 4e21, cap 100%,
        // 4 epochs. Greedy asks 1e24, restrained asks 1e21.
        let mut c = setup();
        let room_id = create(
            &mut c,
            Regime::Paced,
            0,
            10_000,
            4_000_000_000_000_000_000_000,
            60_000_000_000_000_000_000_000,
            4,
        );
        seat_security_and_start(&mut c, room_id, 0);
        let a = alice();
        let b = bob();
        register(&mut c, room_id, &a, false, 0);
        register(&mut c, room_id, &b, false, 0);
        // Epoch 0: snapshot 1.6e22, remaining 4, budget 4e21.
        play_two(&mut c, room_id, 0, 1_000_000_000_000_000_000_000_000, 1_000_000_000_000_000_000_000);
        let view = c.get_epoch(room_id, 0).unwrap();
        assert_eq!(view.total_paid, "3999999999999999999999");
        assert_eq!(view.pool_after, "12000000000000000000001");
        assert_eq!(
            c.get_agent(room_id, a.clone()).unwrap().total_withdrawn,
            "3764705882352941176470"
        );
        assert_eq!(
            c.get_agent(room_id, b.clone()).unwrap().total_withdrawn,
            "235294117647058823529"
        );
        // Epoch 1: snapshot 16000000000000000000001, remaining 3,
        // budget 5333333333333333333333.
        play_two(&mut c, room_id, 1, 1_000_000_000_000_000_000_000_000, 1_000_000_000_000_000_000_000);
        let view = c.get_epoch(room_id, 1).unwrap();
        assert_eq!(view.total_paid, "5333333333333333333332");
        assert_eq!(view.pool_after, "10666666666666666666669");
    }

    #[test]
    #[should_panic(expected = "bio too long")]
    fn profile_bio_too_long_rejected() {
        let mut c = setup();
        let a = alice();
        testing_env!(ctx(&a, 0, T0).build());
        c.set_profile("Alice".into(), "x".repeat(161), "".into());
    }

    // ---------- v3 economics tests ----------

    const FEE_RICH: u128 = 10_000_000_000_000_000_000_000; // 0.01 NEAR

    fn setup_rich() -> Commons {
        testing_env!(ctx(&owner(), 0, T0).build());
        Commons::new(owner(), U128(FEE_RICH))
    }

    /// Create a room on the rich setup (room fee 0.01 NEAR) and
    /// return (room_id, gf). Epoch length is 100 s, as in create().
    fn create_rich(
        c: &mut Commons,
        capacity: u32,
        stake: u128,
        cap_bps: u32,
        refill: u128,
        seed: u128,
        season_epochs: u64,
    ) -> (u64, u128) {
        create_rich2(
            c,
            capacity,
            stake,
            cap_bps,
            refill,
            seed,
            season_epochs,
            Coverage::Covered,
            RefillMode::Flat,
            DEFAULT_DECAY_BPS,
            0,
        )
    }

    /// Full-knob rich create. Returns (room_id, gf); gf is 0 for a
    /// Bare room, whose deposit is fee + seed only.
    #[allow(clippy::too_many_arguments)]
    fn create_rich2(
        c: &mut Commons,
        capacity: u32,
        stake: u128,
        cap_bps: u32,
        refill: u128,
        seed: u128,
        season_epochs: u64,
        coverage: Coverage,
        refill_mode: RefillMode,
        decay_bps: u32,
        refill_floor: u128,
    ) -> (u64, u128) {
        let gf = match coverage {
            Coverage::Covered => guarantee_fund_for(capacity, season_epochs, GAS_PER_EPOCH),
            Coverage::Bare => 0,
        };
        testing_env!(ctx(&sponsor(), FEE_RICH + seed + gf, T0).build());
        let id = c.create_room(
            "rich".to_string(),
            Regime::Wild,
            U128(stake),
            capacity,
            U128(seed),
            U128(refill),
            cap_bps,
            100,
            season_epochs,
            coverage,
            refill_mode,
            decay_bps,
            U128(refill_floor),
        );
        (id, gf)
    }

    /// One feeder plays one full epoch alone with the given request.
    fn play_one(c: &mut Commons, room: u64, epoch: u64, who: &AccountId, amount: u128) {
        let base = T0 + epoch * 100 * SEC;
        testing_env!(ctx(who, 0, base + 10 * SEC).build());
        c.commit(room, commitment_for(amount, "r", "s"));
        testing_env!(ctx(who, 0, base + 60 * SEC).build());
        c.reveal(room, U128(amount), "r".into(), "".into(), "s".into());
        testing_env!(ctx(&sponsor(), 0, base + 101 * SEC).build());
        c.settle_epoch(room, epoch);
    }

    fn finalize_at(c: &mut Commons, room: u64, ts: u64) {
        testing_env!(ctx(&sponsor(), 0, ts).build());
        c.finalize_room(room);
    }

    #[test]
    fn config_exposes_v3_params() {
        let c = setup();
        let cfg = c.get_config();
        assert_eq!(cfg.gas_per_epoch, "63300000000000000000");
        assert_eq!(cfg.retainer_per_security, "2000000000000000000000");
    }

    #[test]
    #[should_panic(expected = "guarantee_fund")]
    fn create_room_rejects_deposit_missing_gf() {
        let mut c = setup_rich();
        // A Covered room created with the v2 deposit (fee + seed,
        // no GF) must be rejected.
        testing_env!(ctx(&sponsor(), FEE_RICH + 20_000, T0).build());
        c.create_room(
            "x".to_string(),
            Regime::Wild,
            U128(0),
            10,
            U128(20_000),
            U128(0),
            1_000,
            100,
            3,
            Coverage::Covered,
            RefillMode::Flat,
            DEFAULT_DECAY_BPS,
            U128(0),
        );
    }

    #[test]
    fn gf_computed_exactly_and_stored() {
        // 10 feeders x 100 epochs x G x 3/2 = 0.095 NEAR exactly.
        assert_eq!(
            guarantee_fund_for(10, 100, GAS_PER_EPOCH),
            94_950 * 10u128.pow(18)
        );
        let mut c = setup_rich();
        let (room_id, gf) = create_rich(&mut c, 10, 0, 1_000, 0, 20_000, 3);
        // 10 x 3 plays, times G, times 3/2 = 45 x G.
        assert_eq!(gf, 45 * GAS_PER_EPOCH);
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.guarantee_fund, gf.to_string());
        assert_eq!(room.gf_remaining, gf.to_string());
        // Retainer reserve = min(fee, 5 x retainer) = min(0.01,
        // 0.01) NEAR: with these config values the whole fee is held.
        assert_eq!(room.retainer_reserve, FEE_RICH.to_string());
    }

    #[test]
    fn topup_zero_revealer_exact() {
        // The dust case: feeder A reveals amount 0 in both epochs of
        // a 2-epoch season, earns nothing from the pool, and must be
        // topped up at finalize to exactly 2 x G. v4: gas only, no
        // topup_eps on top (in v3 this expected 2 x G + eps).
        let mut c = setup_rich();
        let seed = 20_000_000_000_000_000_000_000; // 0.02 NEAR
        let (room_id, gf) = create_rich(&mut c, 10, 0, 1_000, 0, seed, 2);
        seat_security_and_start(&mut c, room_id, 0);
        let a = alice();
        register(&mut c, room_id, &a, false, 0);
        play_one(&mut c, room_id, 0, &a, 0);
        play_one(&mut c, room_id, 1, &a, 0);
        let agent = c.get_agent(room_id, a.clone()).unwrap();
        assert_eq!(agent.epochs_revealed, 2);
        assert_eq!(agent.total_withdrawn, "0");
        finalize_at(&mut c, room_id, T0 + 201 * SEC);
        let expected_topup = 2 * GAS_PER_EPOCH;
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.gf_remaining, (gf - expected_topup).to_string());
    }

    #[test]
    fn topup_zero_when_payouts_exceed() {
        // Feeder B asks a normal amount and is paid far more than
        // 1 x G, so finalize must not touch the GF for him.
        let mut c = setup_rich();
        let seed = 4_000_000_000_000_000_000_000_000; // 4 NEAR
        let (room_id, gf) = create_rich(&mut c, 10, 0, 10_000, 0, seed, 1);
        seat_security_and_start(&mut c, room_id, 0);
        let b = bob();
        register(&mut c, room_id, &b, false, 0);
        let ask = 100_000_000_000_000_000_000_000; // 0.1 NEAR
        play_one(&mut c, room_id, 0, &b, ask);
        assert_eq!(c.get_agent(room_id, b).unwrap().total_withdrawn, ask.to_string());
        finalize_at(&mut c, room_id, T0 + 101 * SEC);
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.gf_remaining, gf.to_string());
    }

    #[test]
    fn topup_none_for_early_exit() {
        // A feeder who plays one epoch and then exits early is
        // inactive at finalize: the guarantee is void, GF untouched.
        let mut c = setup_rich();
        let seed = 20_000_000_000_000_000_000_000;
        let (room_id, gf) = create_rich(&mut c, 10, 0, 1_000, 0, seed, 2);
        seat_security_and_start(&mut c, room_id, 0);
        let a = alice();
        register(&mut c, room_id, &a, false, 0);
        play_one(&mut c, room_id, 0, &a, 0);
        testing_env!(ctx(&a, 0, T0 + 150 * SEC).build());
        c.exit_room(room_id);
        assert!(!c.get_agent(room_id, a.clone()).unwrap().active);
        assert_eq!(c.get_agent(room_id, a).unwrap().epochs_revealed, 1);
        testing_env!(ctx(&sponsor(), 0, T0 + 201 * SEC).build());
        c.settle_epoch(room_id, 1);
        finalize_at(&mut c, room_id, T0 + 201 * SEC);
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.gf_remaining, gf.to_string());
    }

    #[test]
    fn clean_season_pays_retainers() {
        // Clean season, no reports at all: finalize still pays the
        // retainers. The distribution is proven on state by the
        // reserve ending fully distributed (zeroed): 3 active
        // security x 0.002 NEAR paid out of the 0.01 NEAR reserve,
        // the 0.004 NEAR leftover transferred to the owner. (Actual
        // balance deltas are verified in the live smoke, since unit
        // tests do not execute Promise transfers.)
        let mut c = setup_rich();
        let seed = 20_000_000_000_000_000_000_000;
        let (room_id, _gf) = create_rich(&mut c, 10, 0, 1_000, 0, seed, 1);
        seat_security_and_start(&mut c, room_id, 0);
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.retainer_reserve, FEE_RICH.to_string());
        finalize_at(&mut c, room_id, T0 + 101 * SEC);
        let room = c.get_room(room_id).unwrap();
        assert!(room.ended);
        assert_eq!(room.retainer_reserve, "0");
    }

    #[test]
    fn residue_includes_unspent_gf() {
        // Same shape as topup_zero_when_payouts_exceed: the GF is
        // never touched, so withdraw_residue must hand it back with
        // the pool and reserve, zeroing all three fields.
        let mut c = setup_rich();
        let seed = 4_000_000_000_000_000_000_000_000;
        let (room_id, gf) = create_rich(&mut c, 10, 0, 10_000, 0, seed, 1);
        seat_security_and_start(&mut c, room_id, 0);
        let b = bob();
        register(&mut c, room_id, &b, false, 0);
        play_one(&mut c, room_id, 0, &b, 100_000_000_000_000_000_000_000);
        finalize_at(&mut c, room_id, T0 + 101 * SEC);
        let room = c.get_room(room_id).unwrap();
        // Pool 0.8 NEAR - 0.1 paid = 0.7; reserve 3.2; GF intact.
        assert_eq!(room.pool_balance, "700000000000000000000000");
        assert_eq!(room.refill_reserve, "3200000000000000000000000");
        assert_eq!(room.gf_remaining, gf.to_string());
        testing_env!(ctx(&sponsor(), 0, T0 + 102 * SEC).build());
        c.withdraw_residue(room_id);
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.pool_balance, "0");
        assert_eq!(room.refill_reserve, "0");
        assert_eq!(room.gf_remaining, "0");
    }

    #[test]
    fn seed_and_gf_conservation() {
        // Full cycle ledger check, to the yocto. Seed 0.02 NEAR,
        // refill 0.0001 NEAR per epoch, cap 10%, 2 epochs. Alice
        // reveals 0 both epochs; Bob asks 0.001 NEAR both epochs and
        // is capped. Security: 3 seated, clean season.
        let mut c = setup_rich();
        let seed = 20_000_000_000_000_000_000_000u128;
        let refill = 100_000_000_000_000_000_000u128;
        let (room_id, gf) = create_rich(&mut c, 10, 1_000, 1_000, refill, seed, 2);
        seat_security_and_start(&mut c, room_id, 1_000);
        let a = alice();
        let b = bob();
        register(&mut c, room_id, &a, false, 1_000);
        register(&mut c, room_id, &b, false, 1_000);
        let ask_b = 1_000_000_000_000_000_000_000u128; // 0.001 NEAR
        play_two(&mut c, room_id, 0, 0, ask_b);
        play_two(&mut c, room_id, 1, 0, ask_b);
        // Epoch 0: snapshot 0.0041 NEAR, cap 0.00041, Bob paid that.
        // Epoch 1: snapshot 0.00379, cap 0.000379, Bob paid that.
        let total_paid = 789_000_000_000_000_000_000u128;
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.total_paid_out, total_paid.to_string());
        // Seed conservation before finalize: every yocto of the
        // seed is either paid out or still in pool + reserve.
        let pool: u128 = room.pool_balance.parse().unwrap();
        let reserve: u128 = room.refill_reserve.parse().unwrap();
        assert_eq!(pool + reserve + total_paid, seed);
        finalize_at(&mut c, room_id, T0 + 201 * SEC);
        let room = c.get_room(room_id).unwrap();
        // Retainers fully distributed (see the retainer test).
        assert_eq!(room.retainer_reserve, "0");
        // GF conservation, v4 gas-only targets: Alice is topped up
        // to exactly 2G in full; Bob already won 0.000789 NEAR from
        // the pool, far above his 2G target (0.0001266 NEAR), so he
        // is topped up by nothing.
        let topups = 2 * GAS_PER_EPOCH;
        assert_eq!(room.gf_remaining, (gf - topups).to_string());
        // Residue then returns pool + reserve + gf_remaining.
        testing_env!(ctx(&sponsor(), 0, T0 + 202 * SEC).build());
        c.withdraw_residue(room_id);
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.pool_balance, "0");
        assert_eq!(room.refill_reserve, "0");
        assert_eq!(room.gf_remaining, "0");
    }
    // ---------- v4 tests ----------

    fn feeder(i: u32) -> AccountId {
        acct(&format!("feeder{}.testnet", i))
    }

    /// Every listed feeder commits and reveals `amount` in the
    /// epoch, then the epoch is settled.
    fn play_many(
        c: &mut Commons,
        room: u64,
        epoch: u64,
        who: &[AccountId],
        amount: u128,
    ) {
        let base = T0 + epoch * 100 * SEC;
        for (i, w) in who.iter().enumerate() {
            testing_env!(ctx(w, 0, base + 10 * SEC).build());
            c.commit(
                room,
                commitment_for(amount, &format!("r{i}"), &format!("s{i}")),
            );
        }
        for (i, w) in who.iter().enumerate() {
            testing_env!(ctx(w, 0, base + 60 * SEC).build());
            c.reveal(
                room,
                U128(amount),
                format!("r{i}"),
                "".into(),
                format!("s{i}"),
            );
        }
        testing_env!(ctx(&sponsor(), 0, base + 101 * SEC).build());
        c.settle_epoch(room, epoch);
    }

    #[test]
    fn bare_room_no_gf_and_no_topup() {
        // Bare: the create deposit is fee + seed only (create_rich2
        // attaches exactly that and create_room's assert_eq proves
        // no GF was charged), the room stores no fund, and a
        // zero-payout finisher gets NO top-up at finalize: losses
        // are real in a Bare room.
        let mut c = setup_rich();
        let seed = 20_000_000_000_000_000_000_000; // 0.02 NEAR
        let (room_id, gf) = create_rich2(
            &mut c,
            10,
            0,
            1_000,
            0,
            seed,
            2,
            Coverage::Bare,
            RefillMode::Flat,
            DEFAULT_DECAY_BPS,
            0,
        );
        assert_eq!(gf, 0);
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.coverage, "bare");
        assert_eq!(room.guarantee_fund, "0");
        assert_eq!(room.gf_remaining, "0");
        seat_security_and_start(&mut c, room_id, 0);
        let a = alice();
        register(&mut c, room_id, &a, false, 0);
        play_one(&mut c, room_id, 0, &a, 0);
        play_one(&mut c, room_id, 1, &a, 0);
        let agent = c.get_agent(room_id, a.clone()).unwrap();
        assert_eq!(agent.epochs_revealed, 2);
        assert_eq!(agent.total_withdrawn, "0");
        finalize_at(&mut c, room_id, T0 + 201 * SEC);
        // Nothing was topped up: there is no fund to move, and the
        // seed is conserved exactly across pool + reserve (the
        // finisher's gas loss stands, by design).
        let room = c.get_room(room_id).unwrap();
        assert!(room.ended);
        assert_eq!(room.gf_remaining, "0");
        let pool: u128 = room.pool_balance.parse().unwrap();
        let reserve: u128 = room.refill_reserve.parse().unwrap();
        assert_eq!(pool + reserve, seed);
    }

    #[test]
    fn covered_short_season_five_finishers_full_gas() {
        // THE case v3 failed: capacity 5, season 3, five
        // zero-payout full-season finishers. Each is owed exactly
        // 3 x G of gas and nothing more. GF = 5 x 3 x G x 3/2 =
        // 22.5 G covers the total 15 G owed, so every finisher is
        // made whole and gf_remaining drops by exactly 15 G.
        let mut c = setup_rich();
        let seed = 20_000_000_000_000_000_000_000;
        let (room_id, gf) = create_rich(&mut c, 5, 0, 1_000, 0, seed, 3);
        assert_eq!(gf, 45 * GAS_PER_EPOCH / 2);
        // The five finishers are themselves the v5 feeder quorum,
        // so this test seats its own feeders first, then security,
        // then starts (capacity 5 leaves no room for helper seats).
        let feeders: Vec<AccountId> = (1..=5).map(feeder).collect();
        for f in &feeders {
            register(&mut c, room_id, f, false, 0);
        }
        seat_security(&mut c, room_id, 0);
        start(&mut c, room_id);
        for epoch in 0..3u64 {
            play_many(&mut c, room_id, epoch, &feeders, 0);
        }
        for f in &feeders {
            let agent = c.get_agent(room_id, f.clone()).unwrap();
            assert_eq!(agent.epochs_revealed, 3);
            assert_eq!(agent.total_withdrawn, "0");
        }
        finalize_at(&mut c, room_id, T0 + 301 * SEC);
        let room = c.get_room(room_id).unwrap();
        // Exactly 5 x 3 x G left the fund: each finisher received
        // precisely his full gas, no more (no eps) and no less
        // (the fund sufficed, in roster order, to the last one).
        assert_eq!(
            room.gf_remaining,
            (gf - 15 * GAS_PER_EPOCH).to_string()
        );
    }

    #[test]
    fn covered_topup_never_includes_eps() {
        // A finisher's total received from the guarantee equals
        // max(payouts, epochs_revealed x gas), never that plus the
        // old 0.001 NEAR eps. Alice: 2 epochs, zero payouts, so her
        // top-up is exactly 2G; if eps were still in the target the
        // fund would drop by more than the 2G of gas instead.
        let mut c = setup_rich();
        let seed = 20_000_000_000_000_000_000_000;
        let (room_id, gf) = create_rich(&mut c, 10, 0, 1_000, 0, seed, 2);
        seat_security_and_start(&mut c, room_id, 0);
        let a = alice();
        register(&mut c, room_id, &a, false, 0);
        play_one(&mut c, room_id, 0, &a, 0);
        play_one(&mut c, room_id, 1, &a, 0);
        finalize_at(&mut c, room_id, T0 + 201 * SEC);
        let room = c.get_room(room_id).unwrap();
        let dropped = gf - room.gf_remaining.parse::<u128>().unwrap();
        assert_eq!(dropped, 2 * GAS_PER_EPOCH);
        assert!(dropped <= 2 * GAS_PER_EPOCH);
    }

    #[test]
    fn cancel_room_refunds_seed_and_gf_and_exit_is_tax_free() {
        // Covered room cancelled during Recruiting: the sponsor's
        // seed + GF are swept back out (the room's pool, reserve
        // and gf all read 0; unit tests do not execute the Promise
        // transfer itself, as with the other money tests), the fee
        // is not refunded (nothing in the room ever held it beyond
        // the create split), and a registered feeder reclaims its
        // full stake with zero tax: the security pot stays empty.
        let mut c = setup_rich();
        let seed = 20_000_000_000_000_000_000_000;
        let (room_id, gf) = create_rich(&mut c, 10, 1_000, 1_000, 0, seed, 3);
        assert!(gf > 0);
        let a = alice();
        register(&mut c, room_id, &a, false, 1_000);
        register(&mut c, room_id, &sec(1), true, 1_000);
        testing_env!(ctx(&sponsor(), 0, T0 + SEC).build());
        c.cancel_room(room_id);
        let room = c.get_room(room_id).unwrap();
        assert!(room.cancelled);
        assert!(!room.started);
        assert_eq!(room.pool_balance, "0");
        assert_eq!(room.refill_reserve, "0");
        assert_eq!(room.gf_remaining, "0");
        // Feeder exits: full 1000 back, no 10% tax to the pot.
        testing_env!(ctx(&a, 0, T0 + 2 * SEC).build());
        c.exit_room(room_id);
        let agent = c.get_agent(room_id, a).unwrap();
        assert!(!agent.active);
        assert_eq!(agent.stake, "0");
        assert_eq!(c.get_room(room_id).unwrap().security_pot, "0");
        // Security exits on the same terms.
        testing_env!(ctx(&sec(1), 0, T0 + 2 * SEC).build());
        c.exit_room(room_id);
        assert_eq!(c.get_room(room_id).unwrap().security_pot, "0");
    }

    #[test]
    #[should_panic(expected = "room already started")]
    fn cancel_room_after_start_fails() {
        let mut c = setup();
        let room_id = make_room(&mut c, 500, 100, 10_000);
        testing_env!(ctx(&sponsor(), 0, T0 + SEC).build());
        c.cancel_room(room_id);
    }

    #[test]
    #[should_panic(expected = "only the sponsor can cancel")]
    fn cancel_room_rejects_non_sponsor() {
        let mut c = setup();
        let room_id = create(&mut c, Regime::Wild, 1_000, 500, 100, 10_000, 5);
        testing_env!(ctx(&alice(), 0, T0).build());
        c.cancel_room(room_id);
    }

    #[test]
    #[should_panic(expected = "room cancelled")]
    fn start_room_after_cancel_fails() {
        let mut c = setup();
        let room_id = create(&mut c, Regime::Wild, 1_000, 500, 100, 10_000, 5);
        for i in 1..=3 {
            register(&mut c, room_id, &sec(i), true, 1_000);
        }
        testing_env!(ctx(&sponsor(), 0, T0).build());
        c.cancel_room(room_id);
        testing_env!(ctx(&sponsor(), 0, T0).build());
        c.start_room(room_id);
    }

    #[test]
    fn decaying_refill_schedule_and_floor() {
        // Refill 10000, decay 9800 (x0.98), floor 9500. Expected
        // drips: 10000, 9800, 9604, then the raw decay would give
        // 9411 < floor, so 9500, and 9500 again. current_refill
        // after each settle is the NEXT epoch's drip.
        let mut c = setup();
        let room_id = create2(
            &mut c,
            Regime::Wild,
            0,
            1_000,
            10_000,
            1_000_000,
            5,
            Coverage::Covered,
            RefillMode::Decaying,
            DEFAULT_DECAY_BPS,
            9_500,
        );
        seat_security_and_start(&mut c, room_id, 0);
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.refill_mode, "decaying");
        assert_eq!(room.decay_bps, DEFAULT_DECAY_BPS);
        assert_eq!(room.refill_floor, "9500");
        assert_eq!(room.current_refill, "10000");
        let expected_moved = ["10000", "9800", "9604", "9500", "9500"];
        let expected_next = ["9800", "9604", "9500", "9500", "9500"];
        for epoch in 0..5u64 {
            testing_env!(ctx(&sponsor(), 0, T0 + (epoch + 1) * 100 * SEC + SEC).build());
            c.settle_epoch(room_id, epoch);
            let view = c.get_epoch(room_id, epoch).unwrap();
            assert_eq!(view.refill_moved, expected_moved[epoch as usize], "epoch {}", epoch);
            assert_eq!(
                c.get_room(room_id).unwrap().current_refill,
                expected_next[epoch as usize],
                "after epoch {}",
                epoch
            );
        }
    }

    #[test]
    fn flat_refill_stays_constant() {
        // A Flat room under the v4 code path drips exactly
        // refill_per_epoch forever; current_refill never moves.
        let mut c = setup();
        let room_id = create2(
            &mut c,
            Regime::Wild,
            0,
            1_000,
            777,
            1_000_000,
            3,
            Coverage::Covered,
            RefillMode::Flat,
            DEFAULT_DECAY_BPS,
            0,
        );
        seat_security_and_start(&mut c, room_id, 0);
        for epoch in 0..3u64 {
            testing_env!(ctx(&sponsor(), 0, T0 + (epoch + 1) * 100 * SEC + SEC).build());
            c.settle_epoch(room_id, epoch);
            assert_eq!(
                c.get_epoch(room_id, epoch).unwrap().refill_moved,
                "777"
            );
            assert_eq!(c.get_room(room_id).unwrap().current_refill, "777");
        }
    }

    #[test]
    fn bare_room_conservation() {
        // Full-cycle ledger for a Bare room, to the yocto. Seed
        // 0.02 NEAR, refill 0.0001 NEAR, cap 10%, 2 epochs; Alice
        // reveals 0, Bob asks 0.001 NEAR and is capped, exactly as
        // in seed_and_gf_conservation, but with no Guarantee Fund
        // anywhere in the cycle.
        let mut c = setup_rich();
        let seed = 20_000_000_000_000_000_000_000u128;
        let refill = 100_000_000_000_000_000_000u128;
        let (room_id, gf) = create_rich2(
            &mut c,
            10,
            1_000,
            1_000,
            refill,
            seed,
            2,
            Coverage::Bare,
            RefillMode::Flat,
            DEFAULT_DECAY_BPS,
            0,
        );
        assert_eq!(gf, 0);
        seat_security_and_start(&mut c, room_id, 1_000);
        let a = alice();
        let b = bob();
        register(&mut c, room_id, &a, false, 1_000);
        register(&mut c, room_id, &b, false, 1_000);
        let ask_b = 1_000_000_000_000_000_000_000u128;
        play_two(&mut c, room_id, 0, 0, ask_b);
        play_two(&mut c, room_id, 1, 0, ask_b);
        // Same payouts as the Covered twin test: 0.00041 + 0.000379.
        let total_paid = 789_000_000_000_000_000_000u128;
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.total_paid_out, total_paid.to_string());
        let pool: u128 = room.pool_balance.parse().unwrap();
        let reserve: u128 = room.refill_reserve.parse().unwrap();
        assert_eq!(pool + reserve + total_paid, seed);
        assert_eq!(room.guarantee_fund, "0");
        finalize_at(&mut c, room_id, T0 + 201 * SEC);
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.retainer_reserve, "0");
        // No top-ups happened: Alice's zero payouts stand, and the
        // seed is still conserved to the yocto after finalize.
        let pool: u128 = room.pool_balance.parse().unwrap();
        let reserve: u128 = room.refill_reserve.parse().unwrap();
        assert_eq!(pool + reserve + total_paid, seed);
        assert_eq!(room.gf_remaining, "0");
        testing_env!(ctx(&sponsor(), 0, T0 + 202 * SEC).build());
        c.withdraw_residue(room_id);
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.pool_balance, "0");
        assert_eq!(room.refill_reserve, "0");
        assert_eq!(room.gf_remaining, "0");
    }


    // ---------- v6 adversarial tests (audit A1-A4) ----------

    #[test]
    #[should_panic(expected = "amount too large")]
    fn v6_reveal_amount_bounded() {
        // A1: a reveal near the u128 ceiling used to make every
        // settle of the epoch panic forever. It is now rejected
        // at the door.
        let mut c = setup();
        let room_id = make_room(&mut c, 500, 100, 10_000);
        let a = alice();
        register(&mut c, room_id, &a, false, 1_000);
        testing_env!(ctx(&a, 0, T0 + 10 * SEC).build());
        c.commit(room_id, commitment_for(u128::MAX, "r", "s"));
        testing_env!(ctx(&a, 0, T0 + 60 * SEC).build());
        c.reveal(room_id, U128(u128::MAX), "r".into(), "".into(), "s".into());
    }

    #[test]
    fn v6_max_legal_reveals_settle_cleanly() {
        // A1: two feeders reveal the maximum legal amount (10^30
        // yocto each). The settle sums stay far below the u128
        // ceiling and the pool pays out pro-rata as usual.
        let mut c = setup();
        let room_id = make_room(&mut c, 10_000, 0, 10_000);
        let a = alice();
        let b = bob();
        register(&mut c, room_id, &a, false, 1_000);
        register(&mut c, room_id, &b, false, 1_000);
        for (who, s) in [(&a, "sa"), (&b, "sb")] {
            testing_env!(ctx(who, 0, T0 + 10 * SEC).build());
            c.commit(room_id, commitment_for(MAX_REVEAL_YOCTO, "r", s));
        }
        for (who, s) in [(&a, "sa"), (&b, "sb")] {
            testing_env!(ctx(who, 0, T0 + 60 * SEC).build());
            c.reveal(room_id, U128(MAX_REVEAL_YOCTO), "r".into(), "".into(), s.into());
        }
        testing_env!(ctx(&a, 0, T0 + 101 * SEC).build());
        c.settle_epoch(room_id, 0);
        // Pool is 2000 (20% of the 10000 seed), cap 100%: each
        // payable is 2000, the budget is 2000, so each is paid 1000.
        assert_eq!(c.get_agent(room_id, a).unwrap().total_withdrawn, "1000");
        assert_eq!(c.get_agent(room_id, b).unwrap().total_withdrawn, "1000");
        assert_eq!(c.get_room(room_id).unwrap().total_paid_out, "2000");
    }

    #[test]
    #[should_panic(expected = "already committed for this epoch")]
    fn v6_duplicate_commit_rejected() {
        // A4: commit used to silently overwrite (reveal never did).
        let mut c = setup();
        let room_id = make_room(&mut c, 500, 100, 10_000);
        let a = alice();
        register(&mut c, room_id, &a, false, 1_000);
        testing_env!(ctx(&a, 0, T0 + 10 * SEC).build());
        c.commit(room_id, commitment_for(100, "r", "s"));
        testing_env!(ctx(&a, 0, T0 + 20 * SEC).build());
        c.commit(room_id, commitment_for(200, "r", "s"));
    }

    #[test]
    #[should_panic(expected = "epoch out of range for this season")]
    fn v6_ghost_epoch_settle_rejected() {
        // A4: settling epoch == season_epochs (a ghost epoch)
        // used to succeed and write a dataset record.
        let mut c = setup();
        let room_id = make_room(&mut c, 500, 100, 10_000);
        testing_env!(ctx(&sponsor(), 0, T0 + 501 * SEC).build());
        c.settle_epoch(room_id, 5);
    }

    #[test]
    fn v6_finalize_settles_remaining_epochs_itself() {
        // A2: epochs 1..4 are never settled by anyone; finalize
        // after the season end settles them itself and the
        // unsettled epoch's payout still lands.
        let mut c = setup();
        let room_id = make_room(&mut c, 10_000, 0, 10_000);
        let a = alice();
        register(&mut c, room_id, &a, false, 1_000);
        play_one(&mut c, room_id, 0, &a, 100);
        // Epoch 1: commit + reveal, deliberately NO settle.
        testing_env!(ctx(&a, 0, T0 + 110 * SEC).build());
        c.commit(room_id, commitment_for(100, "r", "s"));
        testing_env!(ctx(&a, 0, T0 + 160 * SEC).build());
        c.reveal(room_id, U128(100), "r".into(), "".into(), "s".into());
        finalize_at(&mut c, room_id, T0 + 501 * SEC);
        let room = c.get_room(room_id).unwrap();
        assert!(room.ended);
        assert_eq!(room.next_epoch_to_settle, 5);
        // Both epochs paid 100 each out of the 2000 pool.
        assert_eq!(c.get_agent(room_id, a).unwrap().total_withdrawn, "200");
    }

    #[test]
    fn v6_confirm_after_target_exit_credits_nobody() {
        // A3: the report is filed while the target is active; the
        // target exits before confirmation completes, so no slash
        // happens. The report still confirms, but the reporter
        // must not be credited for it.
        let mut c = setup();
        let room_id = make_room(&mut c, 500, 100, 10_000);
        let feeder = alice();
        register(&mut c, room_id, &feeder, false, 1_000);
        testing_env!(ctx(&sec(1), 0, T0 + 2 * SEC).build());
        let report_id = c.report_abuse(room_id, feeder.clone(), "evidence".into());
        testing_env!(ctx(&feeder, 0, T0 + 3 * SEC).build());
        c.exit_room(room_id);
        testing_env!(ctx(&sec(2), 0, T0 + 4 * SEC).build());
        c.confirm_report(report_id);
        testing_env!(ctx(&sec(3), 0, T0 + 4 * SEC).build());
        c.confirm_report(report_id);
        assert!(c.get_report(report_id).unwrap().confirmed);
        // Pot holds only the 10% exit tax (100); no slash landed.
        assert_eq!(c.get_room(room_id).unwrap().security_pot, "100");
        assert_eq!(
            c.get_agent(room_id, sec(1)).unwrap().confirmed_reports_authored,
            0
        );
    }

    #[test]
    fn v6_threshold_snapshotted_at_report_time() {
        // A3: with 3 security seated the threshold is 2. Two more
        // security register after the report is filed (which would
        // raise a live-computed threshold to 3); two confirms must
        // still confirm the report.
        let mut c = setup();
        let room_id = make_room(&mut c, 500, 100, 10_000);
        let feeder = alice();
        register(&mut c, room_id, &feeder, false, 1_000);
        testing_env!(ctx(&sec(1), 0, T0 + 2 * SEC).build());
        let report_id = c.report_abuse(room_id, feeder.clone(), "evidence".into());
        register(&mut c, room_id, &sec(4), true, 1_000);
        register(&mut c, room_id, &sec(5), true, 1_000);
        testing_env!(ctx(&sec(2), 0, T0 + 3 * SEC).build());
        c.confirm_report(report_id);
        testing_env!(ctx(&sec(3), 0, T0 + 3 * SEC).build());
        c.confirm_report(report_id);
        assert!(c.get_report(report_id).unwrap().confirmed);
        // And this time a slash DID happen, so credit applies.
        assert_eq!(
            c.get_agent(room_id, sec(1)).unwrap().confirmed_reports_authored,
            1
        );
    }

    #[test]
    #[should_panic(expected = "room ended")]
    fn v6_report_after_finalize_rejected() {
        // A3: a report filed into an ended room could be confirmed
        // and strand its slash in a distributed pot.
        let mut c = setup();
        let room_id = make_room(&mut c, 500, 100, 10_000);
        let feeder = alice();
        register(&mut c, room_id, &feeder, false, 1_000);
        finalize_at(&mut c, room_id, T0 + 501 * SEC);
        testing_env!(ctx(&sec(1), 0, T0 + 502 * SEC).build());
        c.report_abuse(room_id, feeder, "evidence".into());
    }

    #[test]
    fn v6_cancel_releases_retainer_reserve() {
        // A4: a cancelled room never finalizes; v5 left the
        // retainer reserve sitting in the room record forever.
        let mut c = setup();
        let room_id = create(&mut c, Regime::Wild, 1_000, 500, 100, 10_000, 5);
        let before: u128 = c.get_room(room_id).unwrap().retainer_reserve.parse().unwrap();
        assert!(before > 0);
        testing_env!(ctx(&sponsor(), 0, T0).build());
        c.cancel_room(room_id);
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.retainer_reserve, "0");
        assert_eq!(room.pool_balance, "0");
    }

    #[test]
    #[should_panic(expected = "epoch_secs out of range")]
    fn v6_create_rejects_tiny_epoch() {
        let mut c = setup();
        testing_env!(ctx(&sponsor(), 10 + 10_000, T0).build());
        c.create_room(
            "test".into(),
            Regime::Wild,
            U128(0),
            10,
            U128(10_000),
            U128(0),
            500,
            10,
            5,
            Coverage::Bare,
            RefillMode::Flat,
            DEFAULT_DECAY_BPS,
            U128(0),
        );
    }

    #[test]
    #[should_panic(expected = "season_epochs out of range")]
    fn v6_create_rejects_huge_season() {
        let mut c = setup();
        testing_env!(ctx(&sponsor(), 10 + 10_000, T0).build());
        c.create_room(
            "test".into(),
            Regime::Wild,
            U128(0),
            10,
            U128(10_000),
            U128(0),
            500,
            100,
            5001,
            Coverage::Bare,
            RefillMode::Flat,
            DEFAULT_DECAY_BPS,
            U128(0),
        );
    }

    #[test]
    #[should_panic(expected = "room name too long")]
    fn v6_create_rejects_long_name() {
        let mut c = setup();
        testing_env!(ctx(&sponsor(), 10 + 10_000, T0).build());
        c.create_room(
            "x".repeat(65),
            Regime::Wild,
            U128(0),
            10,
            U128(10_000),
            U128(0),
            500,
            100,
            5,
            Coverage::Bare,
            RefillMode::Flat,
            DEFAULT_DECAY_BPS,
            U128(0),
        );
    }
}
