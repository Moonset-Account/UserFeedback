"""Synthetic-data tests for :mod:`sample_dispatch`.

Only the standard library is used.  Sample orders are generated in-process;
nothing here touches the network, credentials, or a real training job.

Run status: these tests have NOT been executed in this workspace (see
README.md).  They are provided as executable specifications.
"""

import unittest

from sample_dispatch import (
    CoordinatorError,
    InvalidConfigurationError,
    TrainingCoordinator,
    UnknownRankError,
)


def _complete_step(coordinator, step_index):
    """Fetch for every member, then have every member ack ``step_index``.

    Acknowledging a step that is fully prefetched and fully acked by everyone
    also drains any later step that happens to be complete, so callers must
    tolerate committing more than one step.
    """

    generation = coordinator.generation
    for rank in coordinator.ranks:
        coordinator.fetch(rank, limit=coordinator.max_inflight_steps)
    return [
        coordinator.ack(rank, generation, step_index) for rank in coordinator.ranks
    ]


def _drive_epoch(coordinator, rounds=10000):
    """Advance the commit watermark until the whole epoch is committed."""

    for _ in range(rounds):
        if coordinator.epoch_finished:
            return
        before = coordinator.committed_step_count
        _complete_step(coordinator, before)
        if coordinator.committed_step_count == before and not coordinator.epoch_finished:
            raise AssertionError("epoch made no progress; dispatch is stuck")
    raise AssertionError("epoch did not finish within the round budget")


def _assert_shards_cover_step(testcase, step):
    """Every position of the step is assigned exactly once, in order."""

    covered = []
    for rank, positions in step.shard_positions.items():
        samples = step.shard_samples[rank]
        testcase.assertEqual(len(positions), len(samples))
        for position, sample in zip(positions, samples):
            covered.append((position, sample))
    covered.sort()
    expected = list(zip(range(step.start_position, step.end_position), step.sample_ids))
    testcase.assertEqual(covered, expected)


def _run_membership_change(
    order, batch_size, ranks_before, ranks_after, commit_before, inflight=3
):
    """Commit ``commit_before`` steps, swap membership, then finish the epoch.

    Before the swap every remaining rank has prefetched the next window and the
    step at the commit boundary is partially acknowledged, so the swap has to
    revoke both a half-confirmed step and pure prefetches.
    """

    coordinator = TrainingCoordinator(
        order, batch_size=batch_size, ranks=ranks_before, max_inflight_steps=inflight
    )
    for _ in range(commit_before):
        _complete_step(coordinator, coordinator.committed_step_count)
    if coordinator.committed_step_count != commit_before:
        raise AssertionError("helper committed an unexpected number of steps")
    boundary = coordinator.consumed_positions

    remaining = tuple(rank for rank in coordinator.ranks if rank in ranks_after)
    for rank in remaining:
        coordinator.fetch(rank, limit=inflight)
    generation = coordinator.generation
    for rank in remaining:
        coordinator.ack(rank, generation, commit_before)
    if coordinator.consumed_positions != boundary:
        raise AssertionError(
            "helper expects a partial step at the boundary; at least one rank of "
            "the old membership must be absent from the new one"
        )

    coordinator.set_membership(ranks_after)
    _drive_epoch(coordinator)
    return coordinator, boundary


class RoundRobinDispatchTests(unittest.TestCase):
    def test_positions_cycle_over_ranks_inside_one_global_batch(self):
        coordinator = TrainingCoordinator(range(10), batch_size=4, ranks=[0, 1, 2])
        assignments = {
            rank: coordinator.fetch(rank, limit=1)[0] for rank in coordinator.ranks
        }

        self.assertEqual(assignments[0].positions, (0, 3))
        self.assertEqual(assignments[1].positions, (1,))
        self.assertEqual(assignments[2].positions, (2,))
        for assignment in assignments.values():
            self.assertEqual(assignment.global_start, 0)
            self.assertEqual(assignment.global_end, 4)
            self.assertEqual(assignment.global_batch_size, 4)
            self.assertFalse(assignment.is_empty)

    def test_batch_boundaries_do_not_depend_on_rank_count(self):
        orders = []
        for ranks in ([0], [0, 1], [0, 1, 2], [0, 1, 2, 3]):
            coordinator = TrainingCoordinator(
                range(11), batch_size=4, ranks=ranks, max_inflight_steps=1
            )
            _drive_epoch(coordinator)
            orders.append(tuple(step.sample_ids for step in coordinator.commit_log))

        self.assertEqual(orders[0], ((0, 1, 2, 3), (4, 5, 6, 7), (8, 9, 10)))
        for candidate in orders[1:]:
            self.assertEqual(candidate, orders[0])

    def test_every_rank_sees_the_same_step_boundaries(self):
        coordinator = TrainingCoordinator(range(10), batch_size=3, ranks=[0, 1, 2])
        fetched = {rank: coordinator.fetch(rank, limit=2) for rank in coordinator.ranks}
        for assignments in fetched.values():
            self.assertEqual([a.step_index for a in assignments], [0, 1])
            self.assertEqual(
                [(a.global_start, a.global_end) for a in assignments],
                [(0, 3), (3, 6)],
            )
            self.assertEqual([a.participants for a in assignments], [(0, 1, 2)] * 2)


class PrefetchAndCommitTests(unittest.TestCase):
    def test_rank_can_prefetch_the_next_step(self):
        coordinator = TrainingCoordinator(range(12), batch_size=4, ranks=[0, 1])
        first = coordinator.fetch(0, limit=2)
        self.assertEqual([a.step_index for a in first], [0, 1])
        self.assertEqual(coordinator.committed_step_count, 0)
        # Fetching the same rank again yields nothing new for the same window.
        self.assertEqual(coordinator.fetch(0, limit=2), ())

    def test_prefetch_is_bounded_by_max_inflight_steps(self):
        coordinator = TrainingCoordinator(
            range(20), batch_size=4, ranks=[0, 1], max_inflight_steps=1
        )
        self.assertEqual([a.step_index for a in coordinator.fetch(0, limit=5)], [0])

    def test_steps_commit_only_in_order(self):
        coordinator = TrainingCoordinator(range(12), batch_size=4, ranks=[0, 1])
        for rank in coordinator.ranks:
            coordinator.fetch(rank, limit=2)

        coordinator.ack(0, 1, 1)
        coordinator.ack(1, 1, 1)
        self.assertEqual(coordinator.committed_step_count, 0)
        self.assertEqual(coordinator.pending_step_indices, (0, 1))

        coordinator.ack(0, 1, 0)
        self.assertEqual(coordinator.committed_step_count, 0)
        result = coordinator.ack(1, 1, 0)
        self.assertTrue(result.accepted)
        # The already-confirmed step 1 commits with it, in order.
        self.assertEqual([step.step_index for step in result.committed], [0, 1])
        self.assertEqual(coordinator.consumed_positions, 8)

    def test_lagging_rank_blocks_the_commit_watermark(self):
        coordinator = TrainingCoordinator(range(8), batch_size=4, ranks=[0, 1, 2])
        for rank in coordinator.ranks:
            coordinator.fetch(rank, limit=2)
        coordinator.ack(0, 1, 0)
        coordinator.ack(1, 1, 0)
        self.assertEqual(coordinator.committed_step_count, 0)
        self.assertEqual(coordinator.consumed_positions, 0)
        coordinator.ack(2, 1, 0)
        self.assertEqual(coordinator.committed_step_count, 1)
        self.assertEqual(coordinator.consumed_positions, 4)


class PartialBatchTests(unittest.TestCase):
    def test_empty_shard_still_acknowledges_and_ends_the_epoch(self):
        coordinator = TrainingCoordinator(range(6), batch_size=4, ranks=[0, 1, 2])
        self.assertEqual(coordinator.step_count, 2)

        assignments = {
            rank: coordinator.fetch(rank, limit=2)[1] for rank in coordinator.ranks
        }
        self.assertEqual(assignments[0].positions, (4,))
        self.assertEqual(assignments[1].positions, (5,))
        self.assertTrue(assignments[2].is_empty)
        self.assertEqual(assignments[2].sample_ids, ())
        self.assertTrue(assignments[0].is_last_step)

        _drive_epoch(coordinator)
        self.assertTrue(coordinator.epoch_finished)
        self.assertEqual(coordinator.committed_samples(), tuple(range(6)))
        empty_step = coordinator.commit_log[-1]
        self.assertEqual(empty_step.shard_positions[2], ())
        self.assertTrue(empty_step.last_in_epoch)

    def test_epoch_shorter_than_the_global_batch(self):
        coordinator = TrainingCoordinator(range(2), batch_size=8, ranks=[0, 1, 2, 3])
        _drive_epoch(coordinator)
        self.assertTrue(coordinator.epoch_finished)
        self.assertEqual(coordinator.committed_samples(), (0, 1))
        self.assertEqual(coordinator.commit_log[0].shard_positions[3], ())

    def test_empty_epoch_finishes_immediately(self):
        coordinator = TrainingCoordinator([], batch_size=4, ranks=[0, 1])
        self.assertEqual(coordinator.step_count, 0)
        self.assertTrue(coordinator.epoch_finished)
        self.assertEqual(coordinator.fetch(0, limit=1), ())


class MembershipChangeTests(unittest.TestCase):
    def test_shrink_three_to_two_reproduces_single_rank_order(self):
        # A deliberately non-monotonic epoch order: the coordinator must never
        # sort or re-derive it, only slice it.
        order = tuple("sample-%03d" % ((index * 7) % 23) for index in range(23))
        self.assertEqual(len(set(order)), 23)
        batch_size = 5
        coordinator = TrainingCoordinator(
            order, batch_size=batch_size, ranks=[0, 1, 2], max_inflight_steps=2
        )

        # Two full steps committed: positions [0, 10).
        _complete_step(coordinator, 0)
        _complete_step(coordinator, 1)
        self.assertEqual(coordinator.committed_step_count, 2)
        self.assertEqual(coordinator.consumed_positions, 10)

        # Step 2 is prefetched by everyone, plus step 3, and two of three ranks
        # have already confirmed step 2 when rank 2 leaves.
        old_generation = coordinator.generation
        for rank in coordinator.ranks:
            coordinator.fetch(rank, limit=2)
        self.assertTrue(coordinator.ack(0, old_generation, 2).accepted)
        self.assertTrue(coordinator.ack(1, old_generation, 2).accepted)
        self.assertEqual(coordinator.committed_step_count, 2)
        self.assertEqual(coordinator.pending_step_indices, (2, 3))

        change = coordinator.remove_rank(2)

        self.assertTrue(change.changed)
        self.assertEqual(change.previous_ranks, (0, 1, 2))
        self.assertEqual(change.ranks, (0, 1))
        self.assertEqual(change.revoked_steps, (2, 3))
        self.assertEqual(change.revoked_position_count, 10)
        self.assertEqual(change.resume_position, 10)
        self.assertEqual(change.next_step_index, 2)
        self.assertEqual(coordinator.generation, old_generation + 1)
        self.assertEqual(coordinator.pending_step_indices, ())
        self.assertEqual(coordinator.consumed_positions, 10)

        _drive_epoch(coordinator)
        self.assertTrue(coordinator.epoch_finished)

        # The concatenation of every committed step, in order, equals the
        # single-rank sequential order item for item.
        self.assertEqual(coordinator.committed_samples(), order)
        self.assertEqual(len(coordinator.commit_log), coordinator.step_count)

        # Steps keep the fixed global-batch boundaries; only the rank layout
        # changes.  Steps committed before the change still carry three shards;
        # steps resumed after it alternate between the two remaining ranks.
        resumed_generation = coordinator.generation
        for step in coordinator.commit_log:
            _assert_shards_cover_step(self, step)
            self.assertLessEqual(step.position_count, batch_size)
            if step.generation < resumed_generation:
                self.assertEqual(sorted(step.shard_positions), [0, 1, 2])
                continue
            self.assertEqual(sorted(step.shard_positions), [0, 1])
            for rank, positions in step.shard_positions.items():
                for position in positions:
                    self.assertEqual((position - step.start_position) % 2, rank)

    def test_expand_two_to_three_reproduces_single_rank_order(self):
        order = tuple(range(17))
        coordinator = TrainingCoordinator(
            order, batch_size=4, ranks=[0, 1], max_inflight_steps=2
        )
        _complete_step(coordinator, 0)
        _complete_step(coordinator, 1)
        committed_before = coordinator.consumed_positions
        self.assertEqual(committed_before, 8)

        coordinator.add_rank(2)
        _drive_epoch(coordinator)

        self.assertEqual(coordinator.committed_samples(), order)
        for step in coordinator.commit_log:
            _assert_shards_cover_step(self, step)
        for step in coordinator.commit_log:
            if step.start_position < committed_before:
                self.assertEqual(sorted(step.shard_positions), [0, 1])
            else:
                self.assertEqual(sorted(step.shard_positions), [0, 1, 2])

    def test_all_shrink_configurations_keep_the_sequential_order(self):
        cases = [
            # (total samples, batch size, ranks before, ranks after, steps before)
            (23, 5, (0, 1, 2), (0, 1), 2),
            (31, 6, (0, 1, 2, 3), (0, 2), 2),
            (19, 4, (0, 1, 2, 3, 4), (1, 3, 4), 1),
            (9, 7, (0, 1), (1,), 1),
        ]
        for total, batch_size, before, after, commit_before in cases:
            with self.subTest(total=total, before=before, after=after):
                order = tuple((index * 11) % total for index in range(total))
                self.assertEqual(len(set(order)), total)
                coordinator, boundary = _run_membership_change(
                    order, batch_size, before, after, commit_before
                )

                self.assertTrue(coordinator.epoch_finished)
                self.assertEqual(coordinator.committed_samples(), order)
                self.assertEqual(coordinator.generation, 2)

                new_count = len(after)
                for step in coordinator.commit_log:
                    _assert_shards_cover_step(self, step)
                    if step.end_position <= boundary:
                        self.assertEqual(sorted(step.shard_positions), sorted(before))
                        continue
                    self.assertEqual(sorted(step.shard_positions), sorted(after))
                    for rank, positions in step.shard_positions.items():
                        offset = coordinator.ranks.index(rank)
                        for position in positions:
                            self.assertEqual(
                                (position - step.start_position) % new_count,
                                offset,
                                msg=f"rank {rank} took position {position}",
                            )

    def test_membership_change_revokes_prefetched_steps(self):
        coordinator = TrainingCoordinator(
            range(30), batch_size=4, ranks=[0, 1, 2], max_inflight_steps=3
        )
        for rank in coordinator.ranks:
            coordinator.fetch(rank, limit=3)
        self.assertEqual(coordinator.pending_step_indices, (0, 1, 2))

        change = coordinator.set_membership([0, 1])
        self.assertEqual(change.revoked_steps, (0, 1, 2))
        self.assertEqual(change.revoked_position_count, 12)
        self.assertEqual(change.resume_position, 0)
        self.assertEqual(coordinator.pending_step_indices, ())

        # Re-fetching under the new generation rebuilds the same boundaries.
        rebuilt = coordinator.fetch(0, limit=1)[0]
        self.assertEqual((rebuilt.global_start, rebuilt.global_end), (0, 4))
        self.assertEqual(rebuilt.generation, coordinator.generation)
        self.assertEqual(rebuilt.positions, (0, 2))

    def test_noop_membership_change_keeps_generation(self):
        coordinator = TrainingCoordinator(range(8), batch_size=4, ranks=[0, 1])
        change = coordinator.set_membership([1, 0])
        self.assertFalse(change.changed)
        self.assertEqual(change.generation, 1)
        self.assertEqual(change.revoked_steps, ())

    def test_full_epoch_commit_is_identical_for_every_rank_count(self):
        order = tuple(range(29))
        baseline = TrainingCoordinator(order, batch_size=6, ranks=[0], max_inflight_steps=1)
        _drive_epoch(baseline)
        expected = baseline.committed_samples()
        self.assertEqual(expected, order)

        for ranks in ([0, 1], [0, 1, 2], [0, 1, 2, 3], [0, 1, 2, 3, 4, 5, 6]):
            candidate = TrainingCoordinator(
                order, batch_size=6, ranks=ranks, max_inflight_steps=3
            )
            _drive_epoch(candidate)
            self.assertEqual(candidate.committed_samples(), expected)
            self.assertEqual(
                [s.sample_ids for s in candidate.commit_log],
                [s.sample_ids for s in baseline.commit_log],
            )


class StaleAcknowledgementTests(unittest.TestCase):
    def test_removed_rank_cannot_acknowledge(self):
        coordinator = TrainingCoordinator(range(20), batch_size=4, ranks=[0, 1, 2])
        for rank in coordinator.ranks:
            coordinator.fetch(rank, limit=1)
        coordinator.remove_rank(2)

        result = coordinator.ack(2, 1, 0)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "unknown_rank")
        self.assertEqual(coordinator.committed_step_count, 0)

    def test_old_generation_ack_does_not_count_towards_the_new_quorum(self):
        coordinator = TrainingCoordinator(range(20), batch_size=4, ranks=[0, 1, 2])
        for rank in coordinator.ranks:
            coordinator.fetch(rank, limit=1)
        coordinator.ack(0, 1, 0)
        coordinator.remove_rank(2)
        self.assertEqual(coordinator.generation, 2)

        for rank in coordinator.ranks:
            coordinator.fetch(rank, limit=1)

        stale = coordinator.ack(0, 1, 0)
        self.assertFalse(stale.accepted)
        self.assertEqual(stale.reason, "stale_generation")
        self.assertEqual(coordinator.acks_for(0), frozenset())

        # Rank 2 rejoins, but its old ticket is still from generation 1.
        coordinator.add_rank(2)
        self.assertEqual(coordinator.generation, 3)
        late = coordinator.ack(2, 2, 0)
        self.assertFalse(late.accepted)
        self.assertEqual(late.reason, "stale_generation")

        coordinator.fetch(0, limit=1)
        coordinator.fetch(1, limit=1)
        self.assertEqual(coordinator.ack(0, 3, 0).reason, "accepted")
        self.assertEqual(coordinator.committed_step_count, 0)
        self.assertEqual(coordinator.ack(1, 3, 0).reason, "accepted")
        self.assertEqual(coordinator.committed_step_count, 0)
        coordinator.fetch(2, limit=1)
        result = coordinator.ack(2, 3, 0)
        self.assertEqual(coordinator.committed_step_count, 1)
        self.assertEqual(result.committed[0].sample_ids, (0, 1, 2, 3))

    def test_ack_after_commit_is_rejected(self):
        coordinator = TrainingCoordinator(range(8), batch_size=4, ranks=[0, 1])
        _drive_epoch(coordinator)
        first = coordinator.commit_log[0]
        result = coordinator.ack(0, first.generation, 0)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "already_committed")

    def test_duplicate_ack_is_idempotent(self):
        coordinator = TrainingCoordinator(range(8), batch_size=4, ranks=[0, 1, 2])
        for rank in coordinator.ranks:
            coordinator.fetch(rank, limit=1)
        coordinator.ack(0, 1, 0)
        duplicate = coordinator.ack(0, 1, 0)
        self.assertTrue(duplicate.accepted)
        self.assertEqual(duplicate.reason, "duplicate_ack")
        self.assertEqual(coordinator.committed_step_count, 0)
        coordinator.ack(1, 1, 0)
        coordinator.ack(2, 1, 0)
        self.assertEqual(coordinator.committed_step_count, 1)

    def test_ack_for_unassigned_step_is_rejected(self):
        coordinator = TrainingCoordinator(
            range(20), batch_size=4, ranks=[0, 1], max_inflight_steps=1
        )
        coordinator.fetch(0, limit=1)
        result = coordinator.ack(0, 1, 3)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "not_assigned")

    def test_ack_before_fetching_is_rejected(self):
        coordinator = TrainingCoordinator(range(20), batch_size=4, ranks=[0, 1])
        coordinator.fetch(0, limit=2)
        self.assertEqual(coordinator.pending_step_indices, (0, 1))

        result = coordinator.ack(1, 1, 0)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "not_issued")
        self.assertEqual(coordinator.acks_for(0), frozenset())
        self.assertEqual(coordinator.committed_step_count, 0)

        # After fetching, the same acknowledgement is accepted.
        coordinator.fetch(1, limit=1)
        self.assertEqual(coordinator.ack(1, 1, 0).reason, "accepted")

    def test_rejected_ack_does_not_advance_the_watermark(self):
        coordinator = TrainingCoordinator(range(20), batch_size=4, ranks=[0, 1, 2])
        for rank in coordinator.ranks:
            coordinator.fetch(rank, limit=1)
        coordinator.remove_rank(1)
        coordinator.fetch(0, limit=1)
        coordinator.fetch(2, limit=1)

        for stale_generation in (1, 0, 99):
            result = coordinator.ack(0, stale_generation, 0)
            self.assertFalse(result.accepted)
            self.assertEqual(result.reason, "stale_generation")
        self.assertEqual(coordinator.consumed_positions, 0)
        self.assertEqual(coordinator.pending_step_indices, (0,))

        coordinator.ack(0, 2, 0)
        coordinator.ack(2, 2, 0)
        self.assertEqual(coordinator.consumed_positions, 4)
        self.assertEqual(coordinator.committed_samples(), (0, 1, 2, 3))


class ValidationTests(unittest.TestCase):
    def test_invalid_batch_size(self):
        for value in (0, -1, 1.5, True):
            with self.assertRaises(InvalidConfigurationError):
                TrainingCoordinator(range(4), batch_size=value, ranks=[0])

    def test_invalid_membership(self):
        with self.assertRaises(InvalidConfigurationError):
            TrainingCoordinator(range(4), batch_size=2, ranks=[])
        with self.assertRaises(InvalidConfigurationError):
            TrainingCoordinator(range(4), batch_size=2, ranks=[0, 0])

    def test_unknown_rank_operations(self):
        coordinator = TrainingCoordinator(range(4), batch_size=2, ranks=[0])
        with self.assertRaises(UnknownRankError):
            coordinator.fetch(9)
        with self.assertRaises(UnknownRankError):
            coordinator.remove_rank(9)

    def test_cannot_start_next_epoch_early(self):
        coordinator = TrainingCoordinator(range(8), batch_size=4, ranks=[0, 1])
        with self.assertRaises(CoordinatorError):
            coordinator.start_next_epoch()

    def test_next_epoch_resets_the_watermark(self):
        coordinator = TrainingCoordinator(range(8), batch_size=4, ranks=[0, 1])
        _drive_epoch(coordinator)
        generation = coordinator.generation
        epoch = coordinator.start_next_epoch(range(4))
        self.assertEqual(epoch, 1)
        self.assertEqual(coordinator.generation, generation + 1)
        self.assertEqual(coordinator.consumed_positions, 0)
        self.assertEqual(coordinator.commit_log, ())
        _drive_epoch(coordinator)
        self.assertEqual(coordinator.committed_samples(), (0, 1, 2, 3))


if __name__ == "__main__":
    unittest.main()
