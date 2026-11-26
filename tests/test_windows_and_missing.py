import unittest

from mtu_geometry import (
    PathInformationMissing,
    PathSpec,
    TunnelLayer,
    Window,
    calculate_size_bounds,
    check_commitment,
    common_payload_bounds,
    intersect_windows,
    plan_directions,
    plan_multipath,
    require_committed,
)


def spans(windows):
    return tuple((window.start, window.end) for window in windows)


def make_path(path_id, mtu=1500, windows=(), **kwargs):
    return PathSpec(
        path_id=path_id,
        hops=[("h1", mtu)],
        layers=[TunnelLayer(overhead=40)],
        confirmed_windows=windows,
        **kwargs,
    )


class MissingInformationTests(unittest.TestCase):
    def test_missing_hop_is_named_with_required_mtu_and_bounds(self):
        path = PathSpec(
            path_id="partial",
            hops=[
                ("access", 1500),
                ("unknown-tunnel", None),
                ("core", 1400),
            ],
            layers=[TunnelLayer(overhead=40)],
        )

        report = check_commitment(path, 1300)
        self.assertFalse(report.committed)
        self.assertEqual(report.status, "incomplete")
        self.assertEqual(report.blocker.hop.index, 1)
        self.assertEqual(report.blocker.hop.hop_id, "unknown-tunnel")
        self.assertEqual(report.blocker.required_mtu, path.frame_size(1300))
        self.assertEqual(report.blocker.required_mtu, 1340)

        # The known 1400-byte hop gives a verifiable optimistic ceiling.
        self.assertIsNone(report.size_report.bounds.guaranteed_max)
        self.assertEqual(report.size_report.bounds.optimistic_max, 1360)
        self.assertEqual(report.size_report.bounds.proven_impossible_from, 1361)
        self.assertTrue(all(w.holds for w in report.size_report.bounds.witnesses))

        with self.assertRaises(PathInformationMissing) as context:
            require_committed(path, 1300)
        self.assertEqual(context.exception.report.status, "incomplete")
        self.assertEqual(context.exception.report.blocker.hop.hop_id, "unknown-tunnel")

    def test_known_later_violation_is_reported_as_concrete_blocker(self):
        path = PathSpec(
            path_id="missing-but-also-too-small",
            hops=[("access", 1500), ("unknown", None), ("core", 1400)],
            layers=[TunnelLayer(overhead=40)],
        )
        report = check_commitment(path, 1400)  # frame 1440 > core 1400
        self.assertEqual(report.status, "oversize")
        self.assertEqual(report.blocker.hop.hop_id, "core")
        self.assertEqual(report.blocker.excess, 40)
        self.assertEqual(len(report.missing), 1)

    def test_assumed_floor_gives_explicit_conditional_bound(self):
        path = PathSpec(
            path_id="floor",
            hops=[("known", 1400), ("unknown", None)],
            layers=[TunnelLayer(overhead=40)],
            assumed_floor_mtu=1200,
        )
        bounds = calculate_size_bounds(path).bounds
        self.assertIsNone(bounds.guaranteed_max)
        self.assertEqual(bounds.conditional_floor_mtu, 1200)
        # Unknown hop under the explicit floor allows 1160; known hop allows 1360.
        self.assertEqual(bounds.conditional_floor_max, 1160)

    def test_df_clear_does_not_override_missing_hop(self):
        path = PathSpec(
            path_id="df-clear-missing",
            hops=[("access", 1500), ("unknown", None)],
            layers=[TunnelLayer(overhead=40)],
            dont_fragment=False,
        )
        report = check_commitment(path, 1461)
        self.assertFalse(report.committed)
        self.assertEqual(report.status, "incomplete")
        self.assertEqual(report.blocker.hop.hop_id, "unknown")
        # A conditional fragmentation plan against the known hop is useful
        # evidence, but it must not be treated as a path-wide promise.
        self.assertIsNotNone(report.fragmentation)
        self.assertTrue(report.fragmentation.feasible)


class WindowIntersectionTests(unittest.TestCase):
    def test_partial_overlaps_and_disjoint_pieces(self):
        left = [(0, 10), (20, 30)]
        right = [(5, 15), (25, 35)]
        self.assertEqual(spans(intersect_windows(left, right)), ((5, 10), (25, 30)))

    def test_adjacent_half_open_windows_do_not_create_duration(self):
        self.assertEqual(intersect_windows([(0, 10)], [(10, 20)]), ())

    def test_unsorted_overlapping_windows_are_normalized(self):
        result = intersect_windows([(22, 30), (0, 18)], [(5, 8), (7, 25)])
        self.assertEqual(spans(result), ((5, 18), (22, 25)))

    def test_window_contains_uses_half_open_right_boundary(self):
        window = Window(10, 20)
        self.assertTrue(window.contains(10))
        self.assertTrue(window.contains(19))
        self.assertFalse(window.contains(20))


class MultipathSwitchTests(unittest.TestCase):
    def test_path_windows_are_intersected_with_optional_hop_windows(self):
        path = PathSpec(
            path_id="per-hop-probes",
            hops=[
                ("h1", 1500, [(10, 40), (50, 80)]),
                ("h2", 1500, [(0, 20), (35, 90)]),
                ("h3", 1500),
            ],
            confirmed_windows=[(0, 100)],
        )
        self.assertEqual(
            spans(path.effective_windows()),
            ((10, 20), (35, 40), (50, 80)),
        )

        explicitly_unconfirmed = PathSpec(
            path_id="explicit-empty-hop",
            hops=[("h1", 1500, [])],
            confirmed_windows=[(0, 100)],
        )
        self.assertEqual(explicitly_unconfirmed.effective_windows(), ())

    def test_common_windows_for_same_payload(self):
        path_a = make_path("a", 1500, [(0, 10), (20, 30)])
        path_b = make_path("b", 1500, [(5, 15), (25, 35)])

        plan = plan_multipath(1300, [path_a, path_b])
        self.assertTrue(plan.all_paths_committed)
        self.assertEqual(spans(plan.common_windows), ((5, 10), (25, 30)))
        self.assertEqual(spans(plan.windows_between("a", "b")), ((5, 10), (25, 30)))

    def test_interleaved_three_path_timeline(self):
        paths = [
            make_path("a", windows=[(0, 20)]),
            make_path("b", windows=[(5, 25)]),
            make_path("c", windows=[(10, 30)]),
        ]
        plan = plan_multipath(1300, paths)

        self.assertEqual(spans(plan.common_windows), ((10, 20),))
        self.assertEqual(plan.active_paths(3), ("a",))
        self.assertEqual(plan.active_paths(7), ("a", "b"))
        self.assertEqual(plan.active_paths(15), ("a", "b", "c"))
        self.assertEqual(plan.active_paths(22), ("b", "c"))
        self.assertEqual(plan.active_paths(27), ("c",))
        redundant = plan.redundant_windows(2)
        self.assertEqual(
            [segment.window for segment in redundant],
            [Window(5, 10), Window(10, 20), Window(20, 25)],
        )

    def test_common_payload_bound_names_limiting_path(self):
        wide = make_path("wide", 1500)  # 1460
        narrow = make_path("narrow", 1400)  # 1360

        bound = common_payload_bounds([wide, narrow])
        self.assertEqual(bound.guaranteed_max, 1360)
        self.assertEqual(bound.limiting_path, "narrow")
        self.assertEqual(bound.limiting_hop.hop_id, "h1")

    def test_path_that_cannot_carry_payload_empties_all_path_common(self):
        capable = make_path("capable", 1500, [(0, 100)])
        narrow = make_path("narrow", 1400, [(0, 100)])  # max payload 1360

        plan = plan_multipath(1400, [capable, narrow])
        self.assertFalse(plan.all_paths_committed)
        self.assertEqual(plan.common_windows, ())
        self.assertEqual(spans(plan.eligible_common_windows), ((0, 100),))
        self.assertEqual(len(plan.blockers), 1)
        self.assertEqual(plan.blockers[0].hop.hop_id, "h1")
        self.assertEqual(plan.active_paths(50), ("capable",))
        self.assertEqual(spans(plan.paths[1].windows), ((0, 100),))
        self.assertEqual(plan.paths[1].eligible_windows, ())

    def test_missing_hop_blocker_survives_multipath_plan(self):
        capable = make_path("capable", 1500, [(0, 100)])
        missing = PathSpec(
            path_id="missing-path",
            hops=[("access", 1500), ("mystery", None)],
            layers=[TunnelLayer(overhead=40)],
            confirmed_windows=[(0, 100)],
        )

        plan = plan_multipath(1300, [capable, missing])
        self.assertFalse(plan.all_paths_committed)
        self.assertEqual(plan.common_windows, ())
        self.assertEqual(plan.blockers[0].hop.hop_id, "mystery")
        self.assertEqual(plan.blockers[0].required_mtu, missing.frame_size(1300))
        self.assertEqual(spans(plan.eligible_common_windows), ((0, 100),))

    def test_unconfirmed_path_has_no_schedule_window(self):
        geometry_ok = make_path("confirmed", windows=[(0, 10)])
        no_probe = make_path("unconfirmed", windows=[])
        plan = plan_multipath(1300, [geometry_ok, no_probe])

        self.assertTrue(plan.all_paths_committed)
        self.assertEqual(plan.common_windows, ())
        self.assertEqual(plan.eligible_common_windows, ())
        # The confirmed path remains individually available; the path without
        # any confirmed probe window is absent from the timeline.
        self.assertEqual(plan.active_paths(5), ("confirmed",))
        self.assertEqual(plan.paths[1].windows, ())

    def test_directions_are_asymmetric(self):
        forward = make_path("fwd", 1500, [(0, 20)])
        reverse = make_path("rev", 1200, [(5, 25)])  # max payload 1160

        directional = plan_directions(1100, forward=[forward], reverse=[reverse])
        self.assertEqual(spans(directional.forward.common_windows), ((0, 20),))
        self.assertEqual(spans(directional.reverse.common_windows), ((5, 25),))
        self.assertEqual(
            spans(directional.direction("forward").common_windows), ((0, 20),)
        )

        too_large_reverse = plan_directions(
            1200, forward=[make_path("fwd2", 1500, [(0, 20)])],
            reverse=[make_path("rev2", 1200, [(5, 25)])],
        )
        self.assertTrue(too_large_reverse.forward.all_paths_committed)
        self.assertFalse(too_large_reverse.reverse.all_paths_committed)
        self.assertEqual(too_large_reverse.reverse.common_windows, ())


if __name__ == "__main__":
    unittest.main()
