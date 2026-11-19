"""Acceptance tests for the trajectory publishing library.

Covers the cross constraints of the problem statement:

* safe raw samples are kept and safe links stay connected (acceptable);
* samples/links whose possible position touches an active secret region
  are hidden/cut (rejected);
* closed time intervals, tangential ("touching") contact, linear
  interpolation of coordinates and radius, and interleaved active windows
  (boundary / interleaved scenarios);
* no interpolated boundary points are emitted and removed samples are
  never reconnected.
"""

import unittest

from trajectory_publish import (
    Sample,
    SecretRegion,
    sanitize_trajectory,
)


# Canonical secret region used by many tests: the square [10, 20] x [10, 20]
# that is secret during the closed interval [100, 200].
REGION = SecretRegion(10, 10, 20, 20, 100, 200)


def flat_tracks(result):
    return [sample for track in result.tracks for sample in track]


class TestAcceptableResults(unittest.TestCase):
    """Trajectories that never threaten a secret region pass through intact."""

    def test_no_regions_keeps_everything(self):
        samples = [Sample(t, float(t), 0.0, 1.0) for t in (0, 10, 20, 30)]
        result = sanitize_trajectory(samples, [])
        self.assertEqual(tuple(samples), result.samples)
        self.assertEqual((), result.hidden_samples)
        self.assertEqual((tuple(samples),), result.tracks)
        self.assertEqual(3, len(result.segments))

    def test_far_away_region_keeps_everything(self):
        samples = [Sample(150, 0.0, 0.0, 2.0), Sample(160, 5.0, 0.0, 2.0)]
        result = sanitize_trajectory(samples, [REGION])
        self.assertEqual(tuple(samples), result.samples)
        self.assertEqual((tuple(samples),), result.tracks)

    def test_region_inactive_at_sample_time_keeps_sample(self):
        # Spatially inside the rectangle, but before its active window.
        inside_early = Sample(50, 15.0, 15.0, 1.0)
        # Spatially inside, after the active window.
        inside_late = Sample(250, 15.0, 15.0, 1.0)
        result = sanitize_trajectory([inside_early, inside_late], [REGION])
        self.assertEqual((inside_early, inside_late), result.samples)
        self.assertEqual((), result.hidden_samples)

    def test_segment_crossing_inactive_region_is_safe(self):
        # The link crosses the rectangle spatially, but entirely before the
        # region becomes secret.
        a = Sample(0, 0.0, 15.0, 0.0)
        b = Sample(90, 30.0, 15.0, 0.0)
        result = sanitize_trajectory([a, b], [REGION])
        self.assertEqual(((a, b),), result.segments)
        self.assertEqual(((a, b),), result.tracks)

    def test_empty_input(self):
        result = sanitize_trajectory([], [REGION])
        self.assertEqual((), result.samples)
        self.assertEqual((), result.segments)
        self.assertEqual((), result.tracks)
        self.assertEqual((), result.hidden_samples)

    def test_single_region_shorthand(self):
        spy = Sample(150, 15.0, 15.0, 0.0)
        result = sanitize_trajectory([spy], REGION)  # not wrapped in a list
        self.assertEqual((spy,), result.hidden_samples)


class TestRejectedResults(unittest.TestCase):
    """Anything that may touch an active secret region must be suppressed."""

    def test_sample_inside_active_region_is_hidden(self):
        spy = Sample(150, 15.0, 15.0, 0.0)
        result = sanitize_trajectory([spy], [REGION])
        self.assertEqual((), result.samples)
        self.assertEqual((spy,), result.hidden_samples)
        self.assertEqual((), result.tracks)

    def test_uncertainty_square_reaching_region_hides_sample(self):
        # Centre is outside the rectangle, but the error square overlaps it.
        sample = Sample(150, 8.0, 15.0, 3.0)  # square spans x in [5, 11]
        result = sanitize_trajectory([sample], [REGION])
        self.assertEqual((), result.samples)
        self.assertEqual((sample,), result.hidden_samples)

    def test_segment_crossing_active_region_is_cut(self):
        a = Sample(100, 0.0, 15.0, 0.0)
        b = Sample(200, 30.0, 15.0, 0.0)
        result = sanitize_trajectory([a, b], [REGION])
        self.assertEqual((), result.segments)
        self.assertEqual(((a,), (b,)), result.tracks)

    def test_removed_sample_is_not_reconnected(self):
        a = Sample(0, 0.0, 15.0, 0.0)
        spy = Sample(150, 15.0, 15.0, 0.0)  # hidden: inside active region
        c = Sample(300, 30.0, 15.0, 0.0)
        result = sanitize_trajectory([a, spy, c], [REGION])
        self.assertEqual((a, c), result.samples)
        # No link may skip over the removed sample.
        self.assertEqual((), result.segments)
        self.assertEqual(((a,), (c,)), result.tracks)


class TestBoundaryAndInterleaved(unittest.TestCase):
    """Closed intervals, tangency, interpolation and interleaved windows."""

    def test_closed_interval_start_and_end_hide_sample(self):
        at_start = Sample(100, 15.0, 15.0, 0.0)
        at_end = Sample(200, 15.0, 15.0, 0.0)
        for sample in (at_start, at_end):
            with self.subTest(t=sample.t):
                result = sanitize_trajectory([sample], [REGION])
                self.assertEqual((), result.samples)
                self.assertEqual((sample,), result.hidden_samples)

    def test_just_outside_closed_interval_keeps_sample(self):
        before = Sample(99, 15.0, 15.0, 0.0)
        after = Sample(201, 15.0, 15.0, 0.0)
        result = sanitize_trajectory([before, after], [REGION])
        self.assertEqual((before, after), result.samples)

    def test_tangential_touch_counts_as_touch(self):
        # Square edge exactly on the rectangle edge: x + r == x_min.
        tangent = Sample(150, 8.0, 15.0, 2.0)
        result = sanitize_trajectory([tangent], [REGION])
        self.assertEqual((tangent,), result.hidden_samples)

    def test_marginally_clear_sample_is_kept(self):
        clear = Sample(150, 8.0, 15.0, 1.999)  # x + r = 9.999 < 10
        result = sanitize_trajectory([clear], [REGION])
        self.assertEqual((clear,), result.samples)

    def test_segment_tangent_at_single_instant_is_cut(self):
        # The interpolated point reaches the rectangle corner (10, 10) at
        # exactly t = 50 and only then.
        region = SecretRegion(10, 10, 20, 20, 0, 100)
        a = Sample(0, 15.0, 0.0, 0.0)
        b = Sample(100, 5.0, 20.0, 0.0)
        result = sanitize_trajectory([a, b], [region])
        self.assertEqual((), result.segments)
        self.assertEqual(((a,), (b,)), result.tracks)

    def test_segment_marginally_missing_corner_is_safe(self):
        # Same path but the y coordinate stays just below the corner, so the
        # x-window and y-window of contact no longer overlap in time.
        region = SecretRegion(10, 10, 20, 20, 0, 100)
        a = Sample(0, 15.0, 0.0, 0.0)
        b = Sample(100, 5.0, 19.9, 0.0)
        result = sanitize_trajectory([a, b], [region])
        self.assertEqual(((a, b),), result.segments)

    def test_segment_touching_exactly_at_window_start_is_cut(self):
        # The link reaches the rectangle interior exactly when the region
        # becomes active (closed interval boundary).
        a = Sample(0, 5.0, 15.0, 0.0)
        b = Sample(100, 15.0, 15.0, 0.0)  # at t=100 the point is (15, 15)
        result = sanitize_trajectory([a, b], [REGION])
        self.assertEqual((), result.segments)

    def test_segment_ending_before_window_start_is_safe(self):
        a = Sample(0, 5.0, 15.0, 0.0)
        b = Sample(99, 14.9, 15.0, 0.0)  # link ends before t=100
        result = sanitize_trajectory([a, b], [REGION])
        self.assertEqual(((a, b),), result.segments)

    def test_radius_interpolation_triggers_mid_segment_touch(self):
        # Both endpoint squares are clear; only the interpolated square
        # (growing radius, moving centre) touches the region mid-way.
        region = SecretRegion(10, 10, 20, 20, 0, 100)
        a = Sample(0, 0.0, 15.0, 0.0)
        b = Sample(100, 30.0, 35.0, 5.0)
        # Sanity: endpoints really are clear.
        self.assertEqual((a, b), sanitize_trajectory([a, b], []).samples)
        result = sanitize_trajectory([a, b], [region])
        self.assertEqual((a, b), result.samples)  # endpoints stay
        self.assertEqual((), result.segments)     # but the link is cut
        self.assertEqual(((a,), (b,)), result.tracks)

    def test_safe_endpoints_with_mid_crossing_yield_two_tracks(self):
        # Two safe links separated by one link that crosses the secret
        # region while it is active: the middle link is cut and the result
        # is exactly two displayable trajectory parts.
        s0 = Sample(0, 0.0, 15.0, 0.0)
        s1 = Sample(50, 5.0, 15.0, 0.0)
        s2 = Sample(250, 25.0, 15.0, 0.0)
        s3 = Sample(300, 30.0, 15.0, 0.0)
        result = sanitize_trajectory([s0, s1, s2, s3], [REGION])
        self.assertEqual((s0, s1, s2, s3), result.samples)
        self.assertEqual(((s0, s1), (s2, s3)), result.tracks)
        self.assertEqual(((s0, s1), (s2, s3)), result.segments)

    def test_interleaved_regions_cut_only_during_own_window(self):
        # R1 is secret early, R2 late.  The link crosses R1's rectangle
        # while R1 is inactive (safe), then R2's rectangle while R2 is
        # active (cut).
        r1 = SecretRegion(10, 10, 20, 20, 0, 100)
        r2 = SecretRegion(40, 10, 50, 20, 200, 300)
        a = Sample(150, 15.0, 15.0, 0.0)   # inside R1's rect, R1 inactive
        b = Sample(250, 45.0, 15.0, 0.0)   # inside R2's rect, R2 active
        only_r1 = sanitize_trajectory([a, b], [r1])
        self.assertEqual(((a, b),), only_r1.segments)
        both = sanitize_trajectory([a, b], [r1, r2])
        self.assertEqual((a,), both.samples)
        self.assertEqual((b,), both.hidden_samples)
        self.assertEqual((), both.segments)


class TestOutputIntegrity(unittest.TestCase):
    """The sanitizer must not leak information through fabricated points."""

    def test_output_contains_only_original_samples(self):
        inputs = [
            Sample(0, 0.0, 15.0, 0.0),
            Sample(50, 5.0, 15.0, 0.0),
            Sample(150, 15.0, 15.0, 0.0),   # hidden
            Sample(250, 25.0, 15.0, 0.0),
            Sample(300, 30.0, 15.0, 0.0),
        ]
        result = sanitize_trajectory(inputs, [REGION])
        for sample in flat_tracks(result):
            self.assertTrue(any(sample is raw for raw in inputs))
        for a, b in result.segments:
            self.assertTrue(any(a is raw for raw in inputs))
            self.assertTrue(any(b is raw for raw in inputs))

    def test_tracks_only_join_originally_adjacent_samples(self):
        inputs = [
            Sample(0, 0.0, 15.0, 0.0),
            Sample(50, 5.0, 15.0, 0.0),
            Sample(150, 15.0, 15.0, 0.0),   # hidden
            Sample(250, 25.0, 15.0, 0.0),
            Sample(300, 30.0, 15.0, 0.0),
        ]
        index_of = {id(sample): i for i, sample in enumerate(inputs)}
        result = sanitize_trajectory(inputs, [REGION])
        for track in result.tracks:
            positions = [index_of[id(sample)] for sample in track]
            self.assertEqual(positions, list(range(positions[0], positions[0] + len(positions))))
        # The hidden sample splits the trajectory: no track spans across it.
        for track in result.tracks:
            self.assertNotIn(inputs[2], track)


class TestValidation(unittest.TestCase):
    def test_negative_radius_rejected(self):
        with self.assertRaises(ValueError):
            Sample(0, 0.0, 0.0, -1.0)

    def test_nan_rejected(self):
        with self.assertRaises(ValueError):
            Sample(float("nan"), 0.0, 0.0, 0.0)
        with self.assertRaises(ValueError):
            SecretRegion(0, 0, float("nan"), 1, 0, 1)

    def test_inverted_rectangle_rejected(self):
        with self.assertRaises(ValueError):
            SecretRegion(20, 10, 10, 20, 0, 1)
        with self.assertRaises(ValueError):
            SecretRegion(10, 20, 20, 10, 0, 1)

    def test_inverted_time_interval_rejected(self):
        with self.assertRaises(ValueError):
            SecretRegion(10, 10, 20, 20, 200, 100)

    def test_non_increasing_sample_times_rejected(self):
        a = Sample(10, 0.0, 0.0, 0.0)
        b = Sample(10, 1.0, 0.0, 0.0)
        with self.assertRaises(ValueError):
            sanitize_trajectory([a, b], [])
        c = Sample(5, 2.0, 0.0, 0.0)
        with self.assertRaises(ValueError):
            sanitize_trajectory([a, c], [])


if __name__ == "__main__":
    unittest.main()
