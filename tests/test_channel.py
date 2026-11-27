import unittest

from channel import (
    CANCELLED,
    COMMITTED,
    PENDING,
    RECEIVED,
    SENT,
    TIMED_OUT,
    WITHDRAWN,
    Branch,
    Channel,
    Kernel,
)


def outcomes(kernel):
    """{seq: ActionState} for convenience."""
    return {s.seq: s for s in kernel.snapshots()}


class BufferedChannelTests(unittest.TestCase):
    def test_fifo_order_across_rounds(self):
        ch = Channel("q", capacity=4)
        k = Kernel()
        a = k.register("p", [("send", ch, "a")])
        b = k.register("p", [("send", ch, "b")])
        k.ready()
        k.run()
        self.assertEqual(outcomes(k)[a].state, SENT)
        self.assertEqual(outcomes(k)[b].state, SENT)
        self.assertEqual(ch.snapshot(), ("a", "b"))

        c = k.register("p", [("send", ch, "c")])
        r1 = k.register("c", [("receive", ch)])
        r2 = k.register("c", [("receive", ch)])
        r3 = k.register("c", [("receive", ch)])
        k.ready()
        k.run()
        o = outcomes(k)
        self.assertEqual(o[c].state, SENT)
        self.assertEqual(o[r1].value, "a")
        self.assertEqual(o[r2].value, "b")
        self.assertEqual(o[r3].value, "c")
        self.assertEqual(ch.snapshot(), ())

    def test_send_blocks_when_full(self):
        ch = Channel("q", capacity=1)
        k = Kernel()
        first = k.register("p", [("send", ch, 1)])
        second = k.register("p", [("send", ch, 2)])
        k.run()
        o = outcomes(k)
        self.assertEqual(o[first].state, SENT)
        self.assertEqual(o[second].state, PENDING)
        self.assertEqual(ch.snapshot(), (1,))
        self.assertEqual(k.pending(), [second])

    def test_receive_blocks_when_empty(self):
        ch = Channel("q", capacity=2)
        k = Kernel()
        r = k.register("c", [("receive", ch)])
        k.ready()
        k.run()
        self.assertEqual(outcomes(k)[r].state, PENDING)

    def test_blocked_send_is_retried_after_receiver_drains(self):
        ch = Channel("q", capacity=1)
        k = Kernel()
        k.register("p", [("send", ch, "first")])
        blocked = k.register("p", [("send", ch, "second")])
        k.ready()
        k.run()
        self.assertEqual(outcomes(k)[blocked].state, PENDING)
        self.assertEqual(ch.snapshot(), ("first",))

        r = k.register("c", [("receive", ch)])
        k.ready()
        k.run()
        o = outcomes(k)
        self.assertEqual(o[r].value, "first")
        self.assertEqual(o[blocked].state, SENT)
        self.assertEqual(ch.snapshot(), ("second",))

    def test_buffered_transfer_is_decoupled_no_peer(self):
        ch = Channel("q", capacity=1)
        k = Kernel()
        s = k.register("p", [("send", ch, "v")])
        r = k.register("c", [("receive", ch)])
        k.run()
        o = outcomes(k)
        self.assertIsNone(o[s].peer)
        self.assertIsNone(o[r].peer)
        self.assertEqual(o[r].value, "v")
        self.assertEqual(k.pair_log(), [])

    def test_buffered_values_queue_in_seeded_order(self):
        ch = Channel("q", capacity=3)
        ch.seed("oldest", "middle")
        k = Kernel()
        r1 = k.register("c", [("receive", ch)])
        r2 = k.register("c", [("receive", ch)])
        k.run()
        o = outcomes(k)
        self.assertEqual(o[r1].value, "oldest")
        self.assertEqual(o[r2].value, "middle")

    def test_select_send_into_buffered_uses_branch_order(self):
        full = Channel("full", capacity=1)
        full.seed("occupied")
        free = Channel("free", capacity=1)
        k = Kernel()
        seq = k.register(
            "p",
            [
                ("send", full, "x"),   # blocked: queue is full
                ("send", free, "y"),   # ready
            ],
        )
        k.run()
        st = outcomes(k)[seq]
        self.assertEqual(st.state, SENT)
        self.assertEqual(st.branch, 1)
        self.assertEqual(free.snapshot(), ("y",))
        self.assertEqual(full.snapshot(), ("occupied",))
        self.assertEqual(k.branches_of(seq), (WITHDRAWN, COMMITTED))

    def test_zero_capacity_channel_never_stores(self):
        ch = Channel("u", capacity=0)
        k = Kernel()
        s = k.register("p", [("send", ch, "v")])
        k.run()
        self.assertEqual(outcomes(k)[s].state, PENDING)
        self.assertEqual(ch.snapshot(), ())

    def test_full_send_lands_after_queued_head_is_drained(self):
        # Go-like behaviour: with a full buffer, a blocked sender is only
        # admitted after the receiver frees a slot; FIFO order is preserved.
        ch = Channel("q", capacity=1)
        ch.seed("head")
        k = Kernel()
        blocked = k.register("p", [("send", ch, "tail")])
        r = k.register("c", [("receive", ch)])
        k.run()
        o = outcomes(k)
        self.assertEqual(o[r].value, "head")
        self.assertEqual(o[blocked].state, SENT)
        self.assertEqual(ch.snapshot(), ("tail",))

    def test_buffered_same_beat_order_decides(self):
        ch = Channel("q", capacity=1)
        ch.seed("v")
        k = Kernel()
        r = k.register("c", [("receive", ch)])
        k.timeout(r).ready()
        k.run()
        self.assertEqual(outcomes(k)[r].state, TIMED_OUT)
        self.assertEqual(ch.snapshot(), ("v",))

        k2 = Kernel()
        r2 = k2.register("c", [("receive", ch)])
        k2.ready().timeout(r2)
        k2.run()
        self.assertEqual(outcomes(k2)[r2].state, RECEIVED)
        self.assertEqual(outcomes(k2)[r2].value, "v")
        self.assertEqual(ch.snapshot(), ())


class UnbufferedChannelTests(unittest.TestCase):
    def test_send_and_receive_from_different_callers_pair(self):
        ch = Channel("u")
        k = Kernel()
        s = k.register("producer", [("send", ch, {"n": 1})])
        r = k.register("consumer", [("receive", ch)])
        k.run()
        o = outcomes(k)
        self.assertEqual(o[s].state, SENT)
        self.assertEqual(o[s].value, {"n": 1})
        self.assertEqual(o[r].state, RECEIVED)
        self.assertEqual(o[r].value, {"n": 1})
        self.assertEqual(o[s].peer, r)
        self.assertEqual(o[r].peer, s)
        self.assertEqual(o[s].peer_branch, 0)
        self.assertEqual(len(k.pair_log()), 1)

    def test_single_sender_stays_pending(self):
        ch = Channel("u")
        k = Kernel()
        s = k.register("p", [("send", ch, 1)])
        k.run()
        self.assertEqual(outcomes(k)[s].state, PENDING)

    def test_single_receiver_stays_pending(self):
        ch = Channel("u")
        k = Kernel()
        r = k.register("c", [("receive", ch)])
        k.run()
        self.assertEqual(outcomes(k)[r].state, PENDING)

    def test_same_caller_cannot_pair_across_two_actions(self):
        ch = Channel("u")
        k = Kernel()
        s = k.register("solo", [("send", ch, 1)])
        r = k.register("solo", [("receive", ch)])
        k.run()
        o = outcomes(k)
        self.assertEqual(o[s].state, PENDING)
        self.assertEqual(o[r].state, PENDING)

    def test_select_never_pairs_its_own_two_branches(self):
        ch = Channel("u")
        k = Kernel()
        seq = k.register(
            "solo",
            [
                ("send", ch, "mine"),
                ("receive", ch),
            ],
        )
        k.run()
        self.assertEqual(outcomes(k)[seq].state, PENDING)
        self.assertEqual(k.pair_log(), [])
        self.assertEqual(k.branches_of(seq), (PENDING, PENDING))

    def test_pairing_uses_registration_order_for_peers(self):
        ch = Channel("u")
        k = Kernel()
        r_early = k.register("c1", [("receive", ch)])
        r_late = k.register("c2", [("receive", ch)])
        s = k.register("p", [("send", ch, "v")])
        k.run()
        o = outcomes(k)
        self.assertEqual(o[s].peer, r_early)
        self.assertEqual(o[r_early].value, "v")
        self.assertEqual(o[r_late].state, PENDING)

    def test_two_sends_one_receive_leaves_one_pending(self):
        ch = Channel("u")
        k = Kernel()
        s1 = k.register("p1", [("send", ch, "one")])
        s2 = k.register("p2", [("send", ch, "two")])
        r = k.register("c", [("receive", ch)])
        k.run()
        o = outcomes(k)
        self.assertEqual(o[s1].state, SENT)
        self.assertEqual(o[s2].state, PENDING)
        self.assertEqual(o[r].value, "one")

    def test_channel_identity_matters(self):
        left = Channel("left")
        right = Channel("right")
        k = Kernel()
        s = k.register("p", [("send", left, 1)])
        r = k.register("c", [("receive", right)])
        k.run()
        self.assertEqual(outcomes(k)[s].state, PENDING)
        self.assertEqual(outcomes(k)[r].state, PENDING)

    def test_late_arrival_pairs_with_earlier_pending_action(self):
        ch = Channel("u")
        k = Kernel()
        r = k.register("c", [("receive", ch)])
        k.ready()
        k.run()
        self.assertEqual(outcomes(k)[r].state, PENDING)

        s = k.register("p", [("send", ch, "late")])
        k.ready()
        k.run()
        o = outcomes(k)
        self.assertEqual(o[s].state, SENT)
        self.assertEqual(o[r].state, RECEIVED)
        self.assertEqual(o[r].value, "late")


class SelectionTests(unittest.TestCase):
    def test_select_commits_only_one_branch(self):
        hot = Channel("hot", capacity=1)
        cold = Channel("cold", capacity=1)
        hot.seed("ready")
        k = Kernel()
        seq = k.register(
            "p",
            [
                ("receive", hot),      # branch 0: ready
                ("receive", cold),     # branch 1: not ready, must not fire
            ],
        )
        k.run()
        st = outcomes(k)[seq]
        self.assertEqual(st.state, RECEIVED)
        self.assertEqual(st.branch, 0)
        self.assertEqual(st.value, "ready")
        self.assertEqual(k.branches_of(seq), (COMMITTED, WITHDRAWN))
        self.assertEqual(cold.snapshot(), ())

    def test_branch_order_breaks_ties_within_one_action(self):
        first = Channel("first", capacity=1)
        second = Channel("second", capacity=1)
        first.seed("F")
        second.seed("S")
        k = Kernel()
        seq = k.register(
            "p",
            [
                ("receive", first),
                ("receive", second),
            ],
        )
        k.run()
        st = outcomes(k)[seq]
        self.assertEqual(st.branch, 0)
        self.assertEqual(st.value, "F")
        self.assertEqual(second.snapshot(), ("S",))

    def test_registration_order_breaks_ties_across_actions(self):
        ch = Channel("q", capacity=2)
        ch.seed("only")
        k = Kernel()
        a = k.register("p1", [("receive", ch)])
        b = k.register("p2", [("receive", ch)])
        k.run()
        o = outcomes(k)
        self.assertEqual(o[a].state, RECEIVED)
        self.assertEqual(o[a].value, "only")
        self.assertEqual(o[b].state, PENDING)

    def test_committed_peer_withdraws_its_own_siblings(self):
        # Producer selects between two channels; consumer only listens on one.
        solo = Channel("solo")
        other = Channel("other")
        k = Kernel()
        consumer = k.register("c", [("receive", solo)])
        producer = k.register(
            "p",
            [
                ("send", other, "miss"),   # branch 0: no partner
                ("send", solo, "hit"),     # branch 1: partner exists
            ],
        )
        k.run()
        o = outcomes(k)
        self.assertEqual(o[consumer].value, "hit")
        self.assertEqual(o[producer].branch, 1)
        self.assertEqual(k.branches_of(producer), (WITHDRAWN, COMMITTED))
        self.assertEqual(len(k.pair_log()), 1)
        self.assertEqual(k.pair_log()[0].value, "hit")

    def test_three_way_crossing_selects_commit_exactly_one_pair(self):
        x = Channel("x")
        y = Channel("y")
        k = Kernel()
        a = k.register("A", [("send", x, "ax"), ("receive", y)])
        b = k.register("B", [("receive", x), ("send", y, "by")])
        c = k.register("C", [("receive", x), ("receive", y)])
        k.run()
        o = outcomes(k)
        self.assertEqual(o[a].state, SENT)
        self.assertEqual(o[a].branch, 0)
        self.assertEqual(o[b].state, RECEIVED)
        self.assertEqual(o[b].value, "ax")
        self.assertEqual(o[b].branch, 0)
        self.assertEqual(o[a].peer, b)
        self.assertEqual(o[b].peer, a)
        self.assertEqual(o[c].state, PENDING)
        self.assertEqual(k.branches_of(c), (PENDING, PENDING))
        self.assertEqual(len(k.pair_log()), 1)
        self.assertEqual([p.value for p in k.pair_log()], ["ax"])

    def test_two_independent_pairs_resolve_in_one_round(self):
        p1 = Channel("p1")
        p2 = Channel("p2")
        k = Kernel()
        r1 = k.register("c1", [("receive", p1)])
        r2 = k.register("c2", [("receive", p2)])
        s2 = k.register("s2", [("send", p2, "two")])
        s1 = k.register("s1", [("send", p1, "one")])
        k.run()
        o = outcomes(k)
        self.assertEqual(o[r1].value, "one")
        self.assertEqual(o[r2].value, "two")
        self.assertEqual(sorted(p.value for p in k.pair_log()), ["one", "two"])


class CancelAndTimeoutTests(unittest.TestCase):
    def test_cancel_before_ready_wins(self):
        ch = Channel("u")
        k = Kernel()
        s = k.register("p", [("send", ch, 1)])
        r = k.register("c", [("receive", ch)])
        k.cancel(r).ready()
        k.run()
        o = outcomes(k)
        self.assertEqual(o[r].state, CANCELLED)
        self.assertIsNone(o[r].value)
        self.assertEqual(o[s].state, PENDING)
        self.assertEqual(k.pair_log(), [])
        self.assertTrue(o[r].finished)

    def test_ready_before_cancel_wins(self):
        ch = Channel("u")
        k = Kernel()
        s = k.register("p", [("send", ch, 1)])
        r = k.register("c", [("receive", ch)])
        k.ready().cancel(r)
        k.run()
        o = outcomes(k)
        self.assertEqual(o[r].state, RECEIVED)
        self.assertEqual(o[r].value, 1)
        self.assertEqual(o[s].state, SENT)
        # the late cancel is a no-op and must not take the value back
        self.assertEqual(len(k.pair_log()), 1)

    def test_timeout_before_ready_leaves_partner_pending(self):
        ch = Channel("u")
        k = Kernel()
        s = k.register("p", [("send", ch, "v")])
        r = k.register("c", [("receive", ch)])
        k.timeout(s).ready()
        k.run()
        o = outcomes(k)
        self.assertEqual(o[s].state, TIMED_OUT)
        self.assertIsNone(o[s].value)
        self.assertEqual(o[r].state, PENDING)

    def test_ready_before_timeout_delivers(self):
        ch = Channel("u")
        k = Kernel()
        s = k.register("p", [("send", ch, "v")])
        r = k.register("c", [("receive", ch)])
        k.ready().timeout(s)
        k.run()
        o = outcomes(k)
        self.assertEqual(o[s].state, SENT)
        self.assertEqual(o[r].state, RECEIVED)
        self.assertEqual(o[r].value, "v")

    def test_late_cancel_after_delivery_does_not_duplicate_value(self):
        ch = Channel("q", capacity=1)
        ch.seed("payload")
        k = Kernel()
        r = k.register("c", [("receive", ch)])
        k.ready()
        k.run()
        self.assertEqual(outcomes(k)[r].value, "payload")

        k.cancel(r)
        k.run()
        st = outcomes(k)[r]
        self.assertEqual(st.state, RECEIVED)
        self.assertEqual(st.value, "payload")

        # a second receiver cannot resurrect the already-consumed value
        r2 = k.register("c2", [("receive", ch)])
        k.ready()
        k.run()
        self.assertEqual(outcomes(k)[r2].state, PENDING)
        self.assertEqual(ch.snapshot(), ())

    def test_three_way_cross_candidates_with_late_cancel_no_double_take(self):
        link = Channel("link")
        k = Kernel()
        a = k.register("A", [("send", link, "tok")])
        b = k.register("B", [("receive", link)])
        c = k.register("C", [("receive", link)])
        k.ready()
        k.run()
        o = outcomes(k)
        self.assertEqual(o[b].value, "tok")
        self.assertEqual(o[c].state, PENDING)

        # B has already collected "tok"; cancelling B and C now must not
        # hand the same value out twice or roll back the delivery.
        k.cancel(c).cancel(b).ready()
        k.run()
        after = outcomes(k)
        self.assertEqual(after[b].state, RECEIVED)
        self.assertEqual(after[b].value, "tok")
        self.assertEqual(after[c].state, CANCELLED)
        self.assertEqual(after[a].state, SENT)
        self.assertEqual(len(k.pair_log()), 1)
        self.assertEqual([p.value for p in k.pair_log()], ["tok"])

    def test_cancel_pending_select_leaves_peer_pending(self):
        ch = Channel("u")
        k = Kernel()
        a = k.register("A", [("send", ch, 1)])
        b = k.register("B", [("receive", ch)])
        k.cancel(a).ready()
        k.run()
        o = outcomes(k)
        self.assertEqual(o[a].state, CANCELLED)
        self.assertEqual(o[b].state, PENDING)

    def test_double_cancel_is_idempotent(self):
        ch = Channel("u")
        k = Kernel()
        r = k.register("c", [("receive", ch)])
        k.cancel(r).cancel(r)
        k.run()
        self.assertEqual(outcomes(k)[r].state, CANCELLED)
        self.assertEqual([e.state for e in k.timeline()].count(CANCELLED), 1)

    def test_cancel_of_unknown_seq_is_ignored(self):
        k = Kernel()
        k.register("p", [("send", Channel("u"), 1)])
        k.cancel(999)
        k.run()
        self.assertEqual([s.state for s in k.snapshots()], [PENDING])

    def test_cancelled_action_withdrew_all_its_branches(self):
        ch = Channel("u")
        k = Kernel()
        seq = k.register("p", [("send", ch, 1), ("receive", ch)])
        k.cancel(seq)
        k.run()
        self.assertEqual(k.branches_of(seq), (WITHDRAWN, WITHDRAWN))


class KernelApiTests(unittest.TestCase):
    def test_run_without_settle_honours_event_list_strictly(self):
        ch = Channel("u")
        k = Kernel()
        s = k.register("p", [("send", ch, "v")])
        r = k.register("c", [("receive", ch)])
        # no ready event at all: a strict run must not commit anything
        self.assertEqual([x.state for x in k.run(settle=False)], [PENDING, PENDING])
        self.assertEqual(k.pair_log(), [])

        k.ready()
        o = {x.seq: x for x in k.run(settle=False)}
        self.assertEqual(o[s].state, SENT)
        self.assertEqual(o[r].state, RECEIVED)

    def test_run_without_settle_does_not_revive_cancelled_action(self):
        ch = Channel("u")
        k = Kernel()
        s = k.register("p", [("send", ch, "v")])
        r = k.register("c", [("receive", ch)])
        k.cancel(r).run(settle=False)
        o = outcomes(k)
        self.assertEqual(o[r].state, CANCELLED)
        self.assertEqual(o[s].state, PENDING)

    def test_step_observes_between_events(self):
        ch = Channel("u")
        k = Kernel()
        s = k.register("p", [("send", ch, "v")])
        r = k.register("c", [("receive", ch)])
        k.ready().cancel(r)
        self.assertTrue(k.has_events())
        self.assertTrue(k.step())            # ready: delivery happens now
        self.assertEqual(outcomes(k)[r].value, "v")
        self.assertTrue(k.step())            # cancel: no-op for a finished action
        self.assertFalse(k.has_events())
        self.assertFalse(k.step())
        self.assertEqual(outcomes(k)[r].state, RECEIVED)

    def test_step_cancel_first_prevents_delivery(self):
        ch = Channel("u")
        k = Kernel()
        s = k.register("p", [("send", ch, "v")])
        r = k.register("c", [("receive", ch)])
        k.cancel(r).ready()
        k.step()
        self.assertEqual(outcomes(k)[r].state, CANCELLED)
        k.step()
        self.assertEqual(outcomes(k)[s].state, PENDING)

    def test_register_accepts_branch_objects(self):
        ch = Channel("q", capacity=1)
        k = Kernel()
        seq = k.register("p", [Branch("send", ch, "v")])
        k.run()
        self.assertEqual(outcomes(k)[seq].state, SENT)

    def test_register_accepts_short_tuples(self):
        ch = Channel("u")
        k = Kernel()
        s = k.register("p", [("send", ch)])
        r = k.register("c", [("receive", ch)])
        k.run()
        o = outcomes(k)
        self.assertEqual(o[s].state, SENT)
        self.assertIsNone(o[s].value)
        self.assertEqual(o[r].state, RECEIVED)

    def test_register_rejects_empty_branches(self):
        k = Kernel()
        with self.assertRaises(ValueError):
            k.register("p", [])

    def test_bad_branch_kind_rejected(self):
        ch = Channel("q")
        with self.assertRaises(ValueError):
            Branch("peek", ch)

    def test_negative_capacity_rejected(self):
        with self.assertRaises(ValueError):
            Channel("bad", capacity=-1)

    def test_seed_respects_capacity(self):
        ch = Channel("q", capacity=1)
        ch.seed("a")
        with self.assertRaises(ValueError):
            ch.seed("b")

    def test_unknown_event_rejected(self):
        k = Kernel()
        with self.assertRaises(ValueError):
            k.event("explode")

    def test_cancel_event_requires_seq(self):
        k = Kernel()
        with self.assertRaises(ValueError):
            k.event("cancel")

    def test_pending_actions_report_no_value(self):
        ch = Channel("q", capacity=1)
        k = Kernel()
        seq = k.register("c", [("receive", ch)])
        k.run()
        st = outcomes(k)[seq]
        self.assertEqual(st.state, PENDING)
        self.assertIsNone(st.value)
        self.assertFalse(st.finished)
        self.assertEqual(k.pending(), [seq])

    def test_snapshot_before_run_is_all_pending(self):
        ch = Channel("q", capacity=1)
        k = Kernel()
        s = k.register("p", [("send", ch, 1)])
        r = k.register("c", [("receive", ch)])
        snap = k.snapshots()
        self.assertEqual([x.seq for x in snap], [s, r])
        self.assertTrue(all(x.state == PENDING for x in snap))
        self.assertEqual(ch.snapshot(), ())

    def test_events_are_consumed_once(self):
        ch = Channel("q", capacity=2)
        ch.seed("a", "b")
        k = Kernel()
        r1 = k.register("c1", [("receive", ch)])
        r2 = k.register("c2", [("receive", ch)])
        k.ready()
        k.run()
        o = outcomes(k)
        self.assertEqual((o[r1].value, o[r2].value), ("a", "b"))
        # running again with no queued events changes nothing
        k.run()
        self.assertEqual((outcomes(k)[r1].value, outcomes(k)[r2].value), ("a", "b"))

    def test_results_map_matches_snapshots(self):
        ch = Channel("q", capacity=1)
        k = Kernel()
        k.register("p", [("send", ch, 1)])
        k.run()
        self.assertEqual(
            {s: st.state for s, st in k.results().items()},
            {st.seq: st.state for st in k.snapshots()},
        )

    def test_state_of_unknown_seq_raises(self):
        k = Kernel()
        with self.assertRaises(KeyError):
            k.state_of(42)


if __name__ == "__main__":
    unittest.main()
