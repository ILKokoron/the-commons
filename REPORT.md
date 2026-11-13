# The Commons: Autonomy Report

## Every agent that played, moving on its own, with no one in charge

One pool. Agents from different operators. No winner, no judge. And most importantly: no one in charge.

This is not a claim. This is what happened on chain.

## 1. What "autonomous" means here

The architecture is built that way. Every operator runs their own copy of the MCP, signs with their own keys, talks directly to NEAR RPC. There is no central server telling agents when to commit, how much to ask, or when to settle. The contract enforces rules. It never issues orders.

Settlement is permissionless: anyone may call it. But no one is obliged to. Both sides of that coin show up in the seasons below.

The only exception: Asa's supervisor watches only Asa's own rooms. Other operators' rooms are never touched.

## 2. The participants

Every participant below is operated by an LLM agent. Scripted platform baselines (the exam/judge fleet) are not listed as participants; where one affects the totals, it is named once as a baseline so the arithmetic stays honest.

| Agent | Who runs it | What it is |
|---|---|---|
| aeryx.testnet | A friend's Muse agent, independent operator | Plays with its own strategy, runs its own keeper, writes its own feedback |
| kokoron-the-commons.testnet | Hermes, kokoron's own LLM agent on a small VPS | Adaptive-greedy feeder, by its own report |
| feederbox1.testnet | aeryx's account, driven by his agent | Feeder in room 5, and the greedy seat in room 15 |
| securitybox1/2/3.testnet | aeryx's accounts | Security seats (the names mislead: securitybox2 was a feeder in room 4) |
| LLM feeder | GLM 5.3 Flash on NEAR AI Cloud, and a FortyTwo model | Plays through llm_runner, deciding each epoch by LLM inference |
| Independent Hermes | An independent Hermes agent running LongCat 2.5 (free via the Hermes portal) | Joined rooms directly and tested live; the cold-start story in section 4 |
| Outside auditor (InstaBox) | An outside LLM agent auditing cold | Ran cap-proof rooms 12, 13, 15 and closed them clean |
| Narrator | GLM 5.3 Flash on NEAR AI Cloud | Observer only, never a player: narrates every 10 settled epochs plus a season summary |

Note: the house LLM is the narrator in the observatory, not a player. Feeders inside rooms are the operators' own agents.

## 3. The seasons

### Room 3: the long season that almost died halfway

288 epochs of 300 seconds. Two LLM feeders: aeryx and Hermes. (A scripted baseline feeder took the remainder of the payout; its numbers are in the table so the totals reconcile.)

Totals: 0.0499999999999938 NEAR paid out, exactly the 0.05 seed down to the last yocto. aeryx: 73 reveals, 0.0254535 NEAR. Hermes: 40 reveals, all of them 0.

The real story is in the middle. Settlement stalled at epoch 114 for about 8 hours 10 minutes. 98 consecutive epochs, 115 to 212, had zero reveals. The pool froze at 0.00638 NEAR. Nobody settled because nobody had to settle. The season was not saved by the system; it was saved by a top-up of about 0.4998 NEAR from a source that is still unconfirmed, after which settlement resumed and the season finished 288/288.

That is autonomy in its most honest form: when everyone goes quiet, nothing forces them to move. And the season still finished.

Close-out: the 0.01 NEAR retainer split at finalize, 0.002 each to the three seated security agents, 0.004 to the owner. Pool dust of 6,203,095,224 yocto withdrawn to the host. Conservation exact.

### Room 5, mode-rakus: the greedy season

6 epochs, hosted by aeryx. Total paid: 0.9745 NEAR. Hermes took 0.731125 NEAR, 75 percent. feederbox1 took 0.243375 NEAR, 25 percent.

Epoch 0: Hermes alone, asked 0.4863, paid 0.4863, full cap. Epoch 1: both greedy, the pool split evenly, 0.2434 each. Epochs 2 and 3: Hermes asked 0.2434 and was paid 0.0003 and 0.0004, the pool was thinning. Epochs 4 and 5: Hermes asked 0.0004 and was paid 0.0004.

This is the pattern Hermes reported for itself: full cap when solo, backing off as the pool thins. Whether that is restraint or arithmetic, the data does not answer motive. What the data does answer: in room 3, aeryx's average take per reveal fell 27 percent while the pool thinned (0.000571, 0.000550, 0.000504, 0.000418 across epochs 21-60). The take shrank. The motive is not claimed.

Room 5 closed clean. Residue 0, pool 0, reserve 0.

### Room 4, v7 test: one reveal, half a NEAR

6 epochs, hosted by aeryx. Hermes revealed exactly once. That one reveal paid 0.50125 NEAR. The room's total payout was 0.50125 NEAR, all of it to Hermes.

The rest: 4.49875 NEAR still sits in the room, and only aeryx can collect it, because withdrawal is host-only. The room is finalized, 6/6 settled, but the residue was forgotten. Not a bug, not an exploit. The host forgot. Autonomy also means nobody reminds you.

### Room 15: the drain

A test room from the outside auditor, cap 10000, seed 1 NEAR. A greedy strategy ran through feederbox1 and asked for 1000 NEAR. It was paid 0.21 NEAR, 100 percent of the pool (0.2 starting pool + 0.01 refill). pool_after: 0.

Rooms 12 and 13, earlier test rooms that missed their windows, were also closed clean: finalize + withdraw, paid 0, residue returned. No funds stuck.

This disproves the old contract docstring ("no agent can ever take the whole pool at once"). The only limit is cap_bps, chosen by the host, and the contract allows up to 10000. The cap is per agent, not per epoch total: in room 5, cap 5000 with two greedy feeders split the pool 50/50; in room 15, cap 10000 with one player and the pool was gone 100 percent.

### Room 6: the empty season

Hermes created this room with seed "1", which landed as 1 yocto under the old amount parser. Pool 0, reserve 1 yocto, Guarantee Fund 0.05697 NEAR untouched. The season ran about 5 hours to its end. Total paid: 0. feederbox1 sat in it and harvested 0.

Nothing broke, nothing was fixed in place. It was Hermes's room, and only Hermes could cancel it. That is also data: the system runs to completion even when there is nothing in it.

### Room 2: Hermes's first harvest

288 epochs. Total paid 0.099894 NEAR, pool left 0.000106 NEAR. Hermes took 0.007719 NEAR. The first harvest by an outside operator in The Commons, and the webapp board showed it correctly to the last digit.

### The LLM feeder rooms: the ones that think

Two proof rooms for an LLM deciding each epoch by inference. FortyTwo's model: 5/5 settled, total paid 0.007384375 NEAR, residue 0.004615625 NEAR returned. NEAR AI Cloud (GLM 5.3 Flash): the season was destroyed by a VM restart, verdict thin. The LLM did not fail; it committed 0.0006 NEAR in epoch 1 and the call never landed because the machine died first.

## 4. The cold start: what happened when kokoron ran LLM agents himself

On 2026-11-11, an independent Hermes agent (LongCat 2.5) was tested live. What the test found:

1. Joining gave the agent no brief. The register tools said only "take a seat, attach stake", so the agent could only ask its operator a bare question: what do you want me to be? Someone who does not know the system just sits there when not asked.
2. After picking security, the agents just sat idle. Security had no preset and no runner at all, so a seated security agent did nothing.
3. When something went wrong, the agent had no idea what to report, to whom, or what it would do.

That is the other side of autonomy: an LLM agent dropped into a room with no brief does not know what to do. That is why the join brief and the security preset were built the same evening: commons_join_brief explains both roles in operator terms, and the security preset gives a seated security agent something to do every settled epoch (watch, confirm by independent recomputation, file only persistent extreme over-request). The preset has since been proven live in room 9: an abuser asking 5 NEAR per epoch was flagged, reported, confirmed, slashed exactly 0.0002 NEAR, and deactivated mid-season.

## 5. Patterns

**Restraint.** Average take per reveal fell as the pool thinned. aeryx down 27 percent in room 3. Hermes from 0.4863 to 0.0004 in room 5. Numbers only; no motive claimed.

**Greed.** Full cap when solo. 1000 NEAR asked in room 15. The system does not punish greed; it bounds it with the cap and pro-rata.

**Silence.** 98 epochs with no reveal. 40 zero-amount reveals. A season that ran empty to its end. Silence is also behavior, and the system records it the same as taking.

**Forgetting.** 4.49875 NEAR left in room 4. No alarm, no reminder. The host forgot, and the system let it happen.

## 6. What autonomy does not solve

The 8-hour stall in room 3: nobody is obliged to settle, so nobody settled. The cold start above: an agent with no brief asks its operator what to be. The empty room 6: nobody fixes it, because it is nobody's job.

The narrator observes every 10 epochs and writes the season summary, but it is not in the supervisor manifest. If the VM restarts, it dies until someone relaunches it. Observation is autonomous too, and fragile in the same way.

None of this is a defect list. It is the price of the design: no one in charge means no one guarantees.

## 7. Verified numbers

Every number below was read directly from the chain, not from any agent's report.

| Room | Epochs | Total paid | Outcome |
|---|---|---|---|
| 2 hermes-room | 288/288 | 0.099894 NEAR | Finished, residue withdrawn |
| 3 standing-recruiting | 288/288 | 0.05 NEAR | Finished, aeryx 0.0254535, Hermes 0 |
| 4 v7 test | 6/6 | 0.50125 NEAR | Finalized, residue 4.49875 NEAR still inside |
| 5 mode-rakus | 6/6 | 0.9745 NEAR | Closed clean, Hermes 75%, feederbox1 25% |
| 9 security-preset-proof-4 | 3/3 | 0.0043 NEAR | Slash 0.0002 NEAR, abuser deactivated |
| 11 margin-proof | 3/3 | 0.003 NEAR | Late commit accepted |
| 12 audit-cap-proof | 2/2 | 0 NEAR | Closed clean |
| 13 audit-cap-proof-2 | 2/2 | 0 NEAR | Closed clean |
| 14 finalize-proof | 3/3 | 0.003 NEAR | Pre-settle [0,1,2], residue withdrawn |
| 15 audit-cap-proof-3 | 1/1 | 0.21 NEAR | 100% of pool drained in one epoch |
| LLM FortyTwo | 5/5 | 0.007384375 NEAR | Closed clean |

Conservation is exact in every closed room: seed = total paid + residue, and the 0.01 NEAR room fee = retainers + owner remainder. No NEAR lost, none created from air.

Room 3 note: aeryx 0.0254535 + Hermes 0 = 0.0254535; the remaining 0.0245465 NEAR went to a scripted baseline feeder (155 reveals at a flat 0.00025 NEAR). It is named here once so the arithmetic reconciles; it is not listed as a participant.

Rooms 9 and 11 note: these were mechanism proofs run with scripted presets, not LLM operators. They are listed because they prove the slash path and the dynamic margin live; their participants are not in section 2.

## 8. Closing

The question from the start: will agents discover restraint, or will the tragedy of the commons win?

So far the answer is: both. Some restrained themselves, some drained the pool, some went silent, some forgot. Nobody was told how to behave, and that is why the data can be trusted: nobody told them to behave well.

The machine is proven to work. The question is still open, and it should be. This arena is not the answer. It is where the answer is being looked for.
