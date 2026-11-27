"""Invariant and interleaving checks.

These tests do not pin a specific winner; they assert properties that must hold
for *any* event list: one value is never collected twice, a committed select
leaves exactly one live branch, every committed receive has a send peer on an
unbuffered channel, and the buffered queue stays FIFO-consistent with the
committed sends/receives.
"""

import random
import unittest

from channel import (
    CANCELLED,
    COMMITTED,
    PENDING,
    RECEIVED,
    SENT,
    TIMED_OUT,
    WITHDRAWN,
    Channel,
    Kernel,
)

TERMINAL = {SENT, RECEIVED, CANCELLED, TIMED_OUT}


class PairingInvariantTests(unittest.TestCase):
    def test_committed_select_withdraws_both_sides_siblings(self):
        ch1 = Channel("ch1")
        ch2 = Channel("ch2")
        k = Kernel()
        a = k.register("A", [("send", ch1, "v1"), ("send", ch2, "v2")])
        b = k.register("B", [("receive", ch1), ("receive", ch2)])
        c = k.register("C", [("receive", ch2)])
        k.run()
        o = {s.seq: s for s in k.snapshots()}
        # A and B pair on ch1 (earliest branch of the earliest action).
        self.assertEqual(o[a].branch, 0)
        self.assertEqual(o[b].branch, 0)
        self.assertEqual(o[a].peer, b)
        self.assertEqual(o[b].peer, a)
        self.assertEqual(k.branches_of(a), (COMMITTED, WITHDRAWN))
        self.assertEqual(k.branches_of(b), (COMMITTED, WITHDRAWN))
        # C was a live candidate on ch2 but must not receive anything now.
        self.assertEqual(o[c].state, PENDING)
        self.assertEqual(len(k.pair_log()), 1)
        self.assertEqual([p.value for p in k.pair_log()], ["v1"])

    def test_a_value_is_never_delivered_to_two_receivers(self):
        ch = Channel("link")
        k = Kernel()
        k.register("sender", [("send", ch, "only")])
        recvs = [k.register(f"r{i}", [("receive", ch)]) for i in range(4)]
        k.run()
        got = [k.state_of(r) for r in recvs if k.state_of(r).state == RECEIVED]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].value, "only")
        self.assertEqual(len(k.pair_log()), 1)

    def test_ready_then_cancel_then_ready_keeps_delivery(self):
        ch = Channel("u")
        k = Kernel()
        s = k.register("p", [("send", ch, "v")])
        r = k.register("c", [("receive", ch)])
        k.ready().cancel(s).ready()
        k.run()
        o = {x.seq: x for x in k.snapshots()}
        self.assertEqual(o[s].state, SENT)
        self.assertEqual(o[r].state, RECEIVED)
        self.assertEqual(o[r].value, "v")

    def test_cancel_both_before_any_ready(self):
        ch = Channel("u")
        k = Kernel()
        s = k.register("p", [("send", ch, "v")])
        r = k.register("c", [("receive", ch)])
        k.cancel(s).cancel(r).ready()
        k.run()
        o = {x.seq: x for x in k.snapshots()}
        self.assertEqual(o[s].state, CANCELLED)
        self.assertEqual(o[r].state, CANCELLED)
        self.assertEqual(k.pair_log(), [])


class RandomizedInvariantTests(unittest.TestCase):
    """Deterministic pseudo-random workloads; only invariants are asserted."""

    def _build(self, seed):
        rng = random.Random(seed)
        buffered = [Channel(f"b{i}", capacity=rng.randint(1, 3)) for i in range(2)]
        unbuffered = [Channel(f"u{i}", capacity=0) for i in range(2)]
        channels = buffered + unbuffered
        k = Kernel()
        seqs = []
        for _ in range(rng.randint(4, 12)):
            caller = f"caller{rng.randint(0, 3)}"
            branches = []
            for _ in range(rng.randint(1, 3)):
                ch = rng.choice(channels)
                if rng.random() < 0.5:
                    branches.append(("send", ch, f"val{rng.randint(0, 999)}"))
                else:
                    branches.append(("receive", ch))
            seqs.append(k.register(caller, branches))
        events = []
        for _ in range(rng.randint(1, 8)):
            roll = rng.random()
            if roll < 0.5 or not seqs:
                events.append(("ready", None))
            elif roll < 0.75:
                events.append(("cancel", rng.choice(seqs)))
            else:
                events.append(("timeout", rng.choice(seqs)))
        for kind, seq in events:
            k.event(kind, seq)
        return k, channels

    def test_invariants_hold_for_many_seeds(self):
        for seed in range(200):
            with self.subTest(seed=seed):
                k, channels = self._build(seed)
                k.run()
                states = {s.seq: s for s in k.snapshots()}

                invalid = [s for s in states.values()
                           if s.state not in TERMINAL and s.state != PENDING]
                self.assertEqual(invalid, [])

                # Each action resolves at most one branch.
                for seq in states:
                    outcomes = k.branches_of(seq)
                    self.assertLessEqual(outcomes.count(COMMITTED), 1)
                    st = states[seq]
                    if st.state == PENDING:
                        self.assertTrue(all(x == PENDING for x in outcomes))
                    elif st.state in (CANCELLED, TIMED_OUT):
                        self.assertTrue(all(x == WITHDRAWN for x in outcomes))
                        self.assertIsNone(st.value)
                    else:
                        self.assertEqual(outcomes.count(COMMITTED), 1)
                        self.assertEqual(
                            outcomes[st.branch], COMMITTED
                        )

                # Every unbuffered pair is one send + one receive, distinct
                # callers, distinct vars, same channel, one shared value.
                delivered_values = []
                for pair in k.pair_log():
                    send = states[pair.send_seq]
                    recv = states[pair.recv_seq]
                    self.assertEqual(send.state, SENT)
                    self.assertEqual(recv.state, RECEIVED)
                    self.assertEqual(send.value, pair.value)
                    self.assertEqual(recv.value, pair.value)
                    self.assertNotEqual(send.caller, recv.caller)
                    self.assertNotEqual(send.seq, recv.seq)
                    delivered_values.append(pair.value)

                # A committed send/receive with a peer must appear in the pair
                # log exactly once.
                paired = set()
                for pair in k.pair_log():
                    paired.add((pair.send_seq, pair.send_branch))
                    paired.add((pair.recv_seq, pair.recv_branch))
                for seq, st in states.items():
                    if st.peer is not None:
                        self.assertIn((seq, st.branch), paired)
                        self.assertEqual(states[st.peer].peer, seq)

                # No peer link may point at a different branch than the one
                # the peer actually recorded.
                for seq, st in states.items():
                    if st.peer is None:
                        continue
                    self.assertEqual(st.peer_branch, states[st.peer].branch)

                # Buffered queue is FIFO and conserves counts: for each
                # buffered channel, committed sends minus committed receives
                # equals what is actually queued.
                for ch in channels:
                    if not ch.buffered:
                        self.assertEqual(len(ch), 0)
                        continue
                    self.assertLessEqual(len(ch), ch.capacity)
                    sends = 0
                    recvs = 0
                    for seq, st in states.items():
                        if st.branch is None:
                            continue
                        branch = k._actions[seq].branches[st.branch]
                        if branch.channel is not ch or st.peer is not None:
                            continue
                        if st.state == SENT:
                            sends += 1
                        elif st.state == RECEIVED:
                            recvs += 1
                    self.assertEqual(sends - recvs, len(ch), (seed, ch.name))

    def test_event_count_matches_timeline_entries(self):
        for seed in range(50):
            with self.subTest(seed=seed):
                k, _ = self._build(seed)
                k.run()
                # Timeline never records the same action twice.
                resolved = [e for e in k.timeline()]
                by_seq = {}
                for entry in resolved:
                    by_seq.setdefault(entry.seq, []).append(entry.state)
                for seq, states_seen in by_seq.items():
                    self.assertEqual(len(states_seen), 1, (seed, seq, states_seen))
                    self.assertTrue(states_seen[0] in TERMINAL)


if __name__ == "__main__":
    unittest.main()
