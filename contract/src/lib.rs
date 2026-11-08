//! The Commons: a shared pool that refills slowly, fed on by AI agents
//! from different operators (testnet).
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

#[derive(BorshSerialize, BorshDeserialize, Clone)]
pub struct Room {
    pub id: u64,
    pub sponsor: AccountId,
    pub name: String,
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
    pub start_ns: u64,
    pub security_pot: u128,
    pub total_paid_out: u128,
    pub next_epoch_to_settle: u64,
    pub ended: bool,
    pub pot_distributed: bool,
    pub residue_withdrawn: bool,
    pub data_exclusivity_until_ns: u64,
}

#[derive(BorshSerialize, BorshDeserialize, Clone)]
pub struct AgentRec {
    pub role: Role,
    pub stake: u128,
    pub active: bool,
    pub total_withdrawn: u128,
    pub confirmed_reports_authored: u32,
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

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct Commons {
    pub owner: AccountId,
    pub room_fee: u128,
    pub next_room_id: u64,
    pub next_report_id: u64,
    pub rooms: IterableMap<u64, Room>,
    pub agents: LookupMap<(u64, AccountId), AgentRec>,
    pub commits: LookupMap<(u64, u64, AccountId), String>,
    pub reveals: LookupMap<(u64, u64, AccountId), RevealRec>,
    pub revealers: LookupMap<(u64, u64), Vec<AccountId>>,
    pub epochs: LookupMap<(u64, u64), EpochRec>,
    pub reports: IterableMap<u64, Report>,
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
}

#[derive(Serialize, Deserialize, JsonSchema)]
#[serde(crate = "near_sdk::serde")]
pub struct RoomView {
    pub id: u64,
    pub sponsor: String,
    pub name: String,
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
    pub start_ns: String,
    pub security_pot: String,
    pub total_paid_out: String,
    pub next_epoch_to_settle: u64,
    pub ended: bool,
    pub data_exclusivity_until_ns: String,
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

fn room_view(r: &Room) -> RoomView {
    RoomView {
        id: r.id,
        sponsor: r.sponsor.to_string(),
        name: r.name.clone(),
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
        start_ns: r.start_ns.to_string(),
        security_pot: r.security_pot.to_string(),
        total_paid_out: r.total_paid_out.to_string(),
        next_epoch_to_settle: r.next_epoch_to_settle,
        ended: r.ended,
        data_exclusivity_until_ns: r.data_exclusivity_until_ns.to_string(),
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

#[near]
impl Commons {
    #[init]
    pub fn new(owner: AccountId, room_fee: U128) -> Self {
        Self {
            owner,
            room_fee: room_fee.0,
            next_room_id: 0,
            next_report_id: 0,
            rooms: IterableMap::new(b"r"),
            agents: LookupMap::new(b"a"),
            commits: LookupMap::new(b"c"),
            reveals: LookupMap::new(b"v"),
            revealers: LookupMap::new(b"e"),
            epochs: LookupMap::new(b"p"),
            reports: IterableMap::new(b"t"),
        }
    }

    // ---------- rooms ----------

    #[payable]
    pub fn create_room(
        &mut self,
        name: String,
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
        assert!(stake.0 > 0, "stake must be positive");
        assert!(seed.0 > 0, "seed must be positive");
        let deposit = env::attached_deposit().as_yoctonear();
        assert_eq!(
            deposit,
            self.room_fee + seed.0,
            "deposit must equal room_fee + seed"
        );
        if self.room_fee > 0 {
            Promise::new(self.owner.clone()).transfer(NearToken::from_yoctonear(self.room_fee));
        }
        let pool_start = seed.0 * INITIAL_POOL_BPS / BPS_DENOMINATOR;
        let now = env::block_timestamp();
        let id = self.next_room_id;
        self.next_room_id += 1;
        let room = Room {
            id,
            sponsor: env::predecessor_account_id(),
            name,
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
            start_ns: now,
            security_pot: 0,
            total_paid_out: 0,
            next_epoch_to_settle: 0,
            ended: false,
            pot_distributed: false,
            residue_withdrawn: false,
            data_exclusivity_until_ns: now
                + data_exclusivity_days * 86_400 * NS_PER_SEC,
        };
        self.rooms.insert(id, room);
        env::log_str(&format!("COMMONS room_created id={}", id));
        id
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
        assert!(
            env::block_timestamp() < self.season_end_ns(&room),
            "season over"
        );
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
        self.agents.insert(
            (room_id, caller),
            AgentRec {
                role,
                stake: deposit,
                active: true,
                total_withdrawn: 0,
                confirmed_reports_authored: 0,
            },
        );
    }

    // ---------- epoch flow ----------

    fn season_end_ns(&self, room: &Room) -> u64 {
        room.start_ns + room.epoch_secs * NS_PER_SEC * room.season_epochs
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
            let pay = if total_payable <= pool_snapshot || total_payable == 0 {
                *payable
            } else {
                payable * pool_snapshot / total_payable
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
        let season_over = env::block_timestamp() >= self.season_end_ns(&room);
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
        assert!(
            env::block_timestamp() >= self.season_end_ns(&room),
            "season not over yet"
        );
        room.ended = true;
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
        let amount = room.pool_balance + room.refill_reserve;
        room.pool_balance = 0;
        room.refill_reserve = 0;
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

    // ---------- views ----------

    pub fn get_config(&self) -> ConfigView {
        ConfigView {
            owner: self.owner.to_string(),
            room_fee: self.room_fee.to_string(),
            exit_tax_bps: EXIT_TAX_BPS as u32,
            abuse_slash_bps: ABUSE_SLASH_BPS as u32,
            rooms_created: self.next_room_id,
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
}

#[cfg(test)]
mod tests {
    use super::*;
    use near_sdk::test_utils::{accounts, VMContextBuilder};
    use near_sdk::testing_env;

    const T0: u64 = 1_000_000_000_000; // ns
    const SEC: u64 = NS_PER_SEC;

    fn ctx(account: &AccountId, deposit: u128, ts: u64) -> VMContextBuilder {
        let mut builder = VMContextBuilder::new();
        builder
            .predecessor_account_id(account.clone())
            .attached_deposit(NearToken::from_yoctonear(deposit))
            .block_timestamp(ts);
        builder
    }

    fn setup() -> Commons {
        let owner = accounts(0);
        testing_env!(ctx(&owner, 0, T0).build());
        Commons::new(owner, U128(10))
    }

    fn make_room(c: &mut Commons, cap_bps: u32, refill: u128, seed: u128) -> u64 {
        let sponsor = accounts(1);
        testing_env!(ctx(&sponsor, 10 + seed, T0).build());
        c.create_room(
            "test".to_string(),
            U128(1_000),
            10,
            U128(seed),
            U128(refill),
            cap_bps,
            100,
            5,
            30,
        )
    }

    fn register(c: &mut Commons, room: u64, who: &AccountId, security: bool) {
        testing_env!(ctx(who, 1_000, T0 + SEC).build());
        if security {
            c.register_security(room);
        } else {
            c.register_agent(room);
        }
    }

    #[test]
    fn room_accounting() {
        let mut c = setup();
        let room_id = make_room(&mut c, 500, 100, 10_000);
        let room = c.get_room(room_id).unwrap();
        assert_eq!(room.pool_balance, "2000"); // 20% of seed
        assert_eq!(room.refill_reserve, "8000");
        assert_eq!(room.stake, "1000");
    }

    #[test]
    fn full_epoch_with_cap() {
        let mut c = setup();
        let room_id = make_room(&mut c, 500, 100, 10_000);
        let a = accounts(2);
        let b = accounts(3);
        register(&mut c, room_id, &a, false);
        register(&mut c, room_id, &b, false);

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
        let a = accounts(2);
        let b = accounts(3);
        register(&mut c, room_id, &a, false);
        register(&mut c, room_id, &b, false);
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
        let a = accounts(2);
        register(&mut c, room_id, &a, false);
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
        let feeder = accounts(2);
        let sec1 = accounts(3);
        let sec2 = accounts(4);
        register(&mut c, room_id, &feeder, false);
        register(&mut c, room_id, &sec1, true);
        register(&mut c, room_id, &sec2, true);
        testing_env!(ctx(&sec1, 0, T0 + 2 * SEC).build());
        let report_id = c.report_abuse(room_id, feeder.clone(), "evidence".into());
        // Threshold: security_count 2 -> majority is 2, reporter cannot
        // confirm own report, so sec2 alone is not enough... threshold is
        // (2/2)+1 = 2 confirms, unreachable with 2 security agents.
        // This documents the v1 requirement: at least 3 security agents
        // for a report to confirm. Register a third and confirm twice.
        let sec3 = accounts(5);
        register(&mut c, room_id, &sec3, true);
        testing_env!(ctx(&sec2, 0, T0 + 3 * SEC).build());
        c.confirm_report(report_id);
        testing_env!(ctx(&sec3, 0, T0 + 3 * SEC).build());
        c.confirm_report(report_id);
        let report = c.get_report(report_id).unwrap();
        assert!(report.confirmed);
        let agent = c.get_agent(room_id, feeder).unwrap();
        assert!(!agent.active);
        // 20% of 1000 slashed into the pot.
        assert_eq!(c.get_room(room_id).unwrap().security_pot, "200");
    }
}
