//! The Commons v2: a shared pool that refills slowly, fed on by AI
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
// v3 economics config, set at init (see Commons::new). The values are
// the measured testnet costs from ECONOMICS-V3-PROPOSAL.md section 1.
pub const GAS_PER_EPOCH: u128 = 63_300_000_000_000_000_000; // 0.0000633 NEAR
pub const TOPUP_EPS: u128 = 1_000_000_000_000_000_000_000; // 0.001 NEAR
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
    pub data_exclusivity_days: u64,
    pub data_exclusivity_until_ns: u64,
    // v3 economics: the Guarantee Fund escrowed at create, what is
    // left of it, and the room-fee slice held back for security
    // retainers (zeroed at finalize once fully distributed).
    pub guarantee_fund: u128,
    pub gf_remaining: u128,
    pub retainer_reserve: u128,
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
    pub topup_eps: u128,
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
    pub topup_eps: String,
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
    pub data_exclusivity_until_ns: String,
    pub guarantee_fund: String,
    pub gf_remaining: String,
    pub retainer_reserve: String,
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
        data_exclusivity_until_ns: r.data_exclusivity_until_ns.to_string(),
        guarantee_fund: r.guarantee_fund.to_string(),
        gf_remaining: r.gf_remaining.to_string(),
        retainer_reserve: r.retainer_reserve.to_string(),
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
            topup_eps: TOPUP_EPS,
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
        data_exclusivity_days: u64,
    ) -> u64 {
        assert!(capacity >= 1 && capacity <= 1000, "capacity out of range");
        assert!(cap_bps >= 1 && (cap_bps as u128) <= BPS_DENOMINATOR, "cap_bps out of range");
        assert!(epoch_secs >= 10, "epoch_secs too small");
        assert!(season_epochs >= 1, "season_epochs too small");
        // v2: stake may be 0 (gas-only entry tiers). The seed must
        // still be positive: a room with no pool is not a room.
        assert!(seed.0 > 0, "seed must be positive");
        // v3: the deposit also escrows the Guarantee Fund (see
        // guarantee_fund_for). The sponsor's worst case is fully
        // known at create: room_fee + seed + GF, nothing more can
        // ever be charged to him.
        let guarantee_fund = guarantee_fund_for(capacity, season_epochs, self.gas_per_epoch);
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
            data_exclusivity_days,
            data_exclusivity_until_ns: 0,
            guarantee_fund,
            gf_remaining: guarantee_fund,
            retainer_reserve,
        };
        self.rooms.insert(id, room);
        env::log_str(&format!("COMMONS room_created id={}", id));
        id
    }

    /// Anchor epoch 0 at the current moment and open the season.
    /// Sponsor only. Requires at least MIN_SECURITY_TO_START registered
    /// security agents, because with fewer, no abuse report in this
    /// room could ever reach its confirm threshold.
    pub fn start_room(&mut self, room_id: u64) {
        let caller = env::predecessor_account_id();
        let mut room = self.rooms.get(&room_id).expect("room not found").clone();
        assert_eq!(caller, room.sponsor, "only the sponsor can start the room");
        assert!(!room.ended, "room ended");
        assert!(!room.started, "room already started");
        assert!(
            room.security_count >= MIN_SECURITY_TO_START,
            "need at least {} security agents to start, have {}",
            MIN_SECURITY_TO_START,
            room.security_count
        );
        let now = env::block_timestamp();
        room.started = true;
        room.start_ns = now;
        room.data_exclusivity_until_ns =
            now + room.data_exclusivity_days * 86_400 * NS_PER_SEC;
        self.rooms.insert(room_id, room);
        env::log_str(&format!("COMMONS room_started id={}", room_id));
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
        let mut room = self.rooms.get(&room_id).expect("room not found").clone();
        assert!(room.started, "room not started");
        assert_eq!(epoch, room.next_epoch_to_settle, "epochs settle in order");
        let now = env::block_timestamp();
        let epoch_end = room.start_ns + (epoch + 1) * room.epoch_secs * NS_PER_SEC;
        assert!(now >= epoch_end, "epoch not finished yet");

        // Refill first: this epoch's drip lands before payouts.
        let refill_moved = room.refill_per_epoch.min(room.refill_reserve);
        room.refill_reserve -= refill_moved;
        room.pool_balance += refill_moved;

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
                total_requested += rec.amount;
                let payable = rec.amount.min(cap);
                total_payable += payable;
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
        let season_over = self.season_over(&room, env::block_timestamp());
        let tax = if season_over || room.ended {
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
        // (b) Feeder top-ups: every still-active feeder who settled
        // at least one reveal is brought up to
        // epochs_revealed x gas_per_epoch + topup_eps if their pool
        // payouts fell short of that, paid from the Guarantee Fund.
        // A feeder who never revealed spent no epoch gas and gets
        // nothing; a feeder who exited early or was slashed is
        // inactive and gets nothing (the guarantee is void by
        // design). Top-ups do not count into total_withdrawn, which
        // tracks pool payouts only.
        if room.gf_remaining > 0 {
            for who in &roster {
                if room.gf_remaining == 0 {
                    break;
                }
                if let Some(agent) = self.agents.get(&(room_id, who.clone())) {
                    if agent.active
                        && matches!(agent.role, Role::Feeder)
                        && agent.epochs_revealed > 0
                    {
                        let target = (agent.epochs_revealed as u128) * self.gas_per_epoch
                            + self.topup_eps;
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
        report.confirms.push(caller);
        let threshold = (room.security_count / 2 + 1) as usize;
        if report.confirms.len() >= threshold {
            report.confirmed = true;
            // Slash the target: part of the stake goes to the security
            // pot, the rest is refunded, the feeder is deactivated.
            let mut target = self
                .agents
                .get(&(report.room_id, report.target.clone()))
                .expect("target missing")
                .clone();
            if target.active {
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
            if let Some(mut rep) = self
                .agents
                .get(&(report.room_id, report.reporter.clone()))
                .cloned()
            {
                rep.confirmed_reports_authored += 1;
                self.agents
                    .insert((report.room_id, report.reporter.clone()), rep);
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
            topup_eps: self.topup_eps.to_string(),
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
        // v3: the deposit must be room_fee + seed + GF. The test
        // room fee is 10 (see setup), capacity is 10 below.
        let gf = guarantee_fund_for(10, season_epochs, GAS_PER_EPOCH);
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
            30,
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

    fn seat_security_and_start(c: &mut Commons, room: u64, stake: u128) {
        for i in 1..=3 {
            register(c, room, &sec(i), true, stake);
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
    #[should_panic(expected = "only the sponsor")]
    fn start_gate_rejects_non_sponsor() {
        let mut c = setup();
        let room_id = create(&mut c, Regime::Wild, 1_000, 500, 100, 10_000, 5);
        for i in 1..=3 {
            register(&mut c, room_id, &sec(i), true, 1_000);
        }
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
        let gf = guarantee_fund_for(capacity, season_epochs, GAS_PER_EPOCH);
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
            30,
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
        assert_eq!(cfg.topup_eps, "1000000000000000000000");
        assert_eq!(cfg.retainer_per_security, "2000000000000000000000");
    }

    #[test]
    #[should_panic(expected = "guarantee_fund")]
    fn create_room_rejects_deposit_missing_gf() {
        let mut c = setup_rich();
        // The v2 deposit (fee + seed, no GF) must now be rejected.
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
            30,
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
        // topped up at finalize to exactly 2 x G + eps.
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
        let expected_topup = 2 * GAS_PER_EPOCH + TOPUP_EPS;
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.gf_remaining, (gf - expected_topup).to_string());
    }

    #[test]
    fn topup_zero_when_payouts_exceed() {
        // Feeder B asks a normal amount and is paid far more than
        // 1 x G + eps, so finalize must not touch the GF for him.
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
        // GF conservation: Alice is topped up to 2G + eps in full;
        // Bob is topped up to 2G + eps minus what he already won.
        let target = 2 * GAS_PER_EPOCH + TOPUP_EPS;
        let topups = target + (target - total_paid);
        assert_eq!(room.gf_remaining, (gf - topups).to_string());
        // Residue then returns pool + reserve + gf_remaining.
        testing_env!(ctx(&sponsor(), 0, T0 + 202 * SEC).build());
        c.withdraw_residue(room_id);
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.pool_balance, "0");
        assert_eq!(room.refill_reserve, "0");
        assert_eq!(room.gf_remaining, "0");
    }
}
