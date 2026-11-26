import unittest

from mtu_geometry import (
    Alignment,
    MtuInterval,
    PathSpec,
    StepOverhead,
    TunnelLayer,
    calculate_size_bounds,
    check_commitment,
    maximum_safe_payload,
    verify_bounds,
)


class BasicGeometryTests(unittest.TestCase):
    def test_constant_overhead_gives_exact_boundary(self):
        path = PathSpec(
            path_id="constant",
            hops=[1500],
            layers=[TunnelLayer(overhead=40, name="ipv4-gre")],
        )

        self.assertEqual(maximum_safe_payload(path), 1460)
        ok = check_commitment(path, 1460)
        self.assertTrue(ok.committed)
        self.assertEqual(ok.status, "committed")
        self.assertEqual(ok.frame_size, 1500)

        bad = check_commitment(path, 1461)
        self.assertFalse(bad.committed)
        self.assertEqual(bad.status, "oversize")
        self.assertEqual(bad.blocker.mtu, 1500)
        self.assertEqual(bad.blocker.frame_size, 1501)
        self.assertEqual(bad.blocker.excess, 1)

    def test_step_overhead_jump_is_accounted_at_inclusive_boundary(self):
        overhead = StepOverhead(
            [
                (0, 20),    # payload 0..100
                (101, 80),  # payload >= 101 jumps into a more expensive tier
            ]
        )
        path = PathSpec(path_id="step", hops=[170], layers=[TunnelLayer(overhead)])

        # 100 + 20 = 120, while 101 + 80 = 181 > 170.
        self.assertEqual(path.frame_size(100), 120)
        self.assertEqual(path.frame_size(101), 181)
        self.assertEqual(maximum_safe_payload(path), 100)

        report = check_commitment(path, 101)
        self.assertFalse(report.committed)
        self.assertEqual(report.blocker.frame_size, 181)
        self.assertEqual(report.blocker.excess, 11)

    def test_upper_bound_constructor_boundary(self):
        overhead = StepOverhead.by_upper_bounds(
            [
                (128, 10),
                (256, 30),
                (None, 50),
            ]
        )
        self.assertEqual(overhead(128), 10)
        self.assertEqual(overhead(129), 30)
        self.assertEqual(overhead(256), 30)
        self.assertEqual(overhead(257), 50)

    def test_nested_layers_compose_inner_to_outer(self):
        def outer_overhead(inner_size: int) -> int:
            return 10 if inner_size <= 100 else 40

        path = PathSpec(
            path_id="nested",
            hops=[130],
            layers=[
                TunnelLayer(overhead=20, name="inner"),
                TunnelLayer(overhead=outer_overhead, name="outer"),
            ],
        )

        # At payload 80, inner size is 100 and outer remains in cheap tier.
        self.assertEqual(path.frame_size(80), 110)
        # At payload 81, outer observes 101, so its overhead jumps to 40.
        self.assertEqual(path.frame_size(81), 141)
        self.assertEqual(maximum_safe_payload(path), 80)

    def test_transmission_alignment_rounds_final_frame(self):
        path = PathSpec(path_id="aligned", hops=[1500], alignment=Alignment(8))

        self.assertEqual(path.frame_size(1496), 1496)
        self.assertEqual(path.frame_size(1497), 1504)
        self.assertEqual(maximum_safe_payload(path), 1496)

    def test_alignment_and_overhead_together(self):
        path = PathSpec(
            path_id="aligned-overhead",
            hops=[1500],
            layers=[TunnelLayer(overhead=40)],
            alignment=8,
        )
        self.assertEqual(path.frame_size(1456), 1496)
        self.assertEqual(path.frame_size(1457), 1504)
        self.assertEqual(maximum_safe_payload(path), 1456)

    def test_multihop_uses_actual_smallest_capacity(self):
        path = PathSpec(
            path_id="three-hops",
            hops=[("access", 1500), ("core", 1400), ("edge", 1500)],
            layers=[TunnelLayer(overhead=40)],
        )

        bounds = calculate_size_bounds(path)
        self.assertEqual(bounds.bounds.guaranteed_max, 1360)
        self.assertEqual(bounds.bottleneck.hop_id, "core")

        # Payload 1400/frame 1440 passes access but is blocked by core.
        report = check_commitment(path, 1400)
        self.assertEqual(report.status, "oversize")
        self.assertEqual(report.blocker.hop.hop_id, "core")

        # A very large frame is first rejected by the first forwarding hop.
        first_hop_report = check_commitment(path, 1461)
        self.assertEqual(first_hop_report.blocker.hop.index, 0)
        self.assertEqual(bounds.bottleneck.index, 1)

    def test_mtu_interval_gives_guaranteed_and_optimistic_bounds(self):
        path = PathSpec(
            path_id="interval",
            hops=[("h", MtuInterval(1400, 1500))],
            layers=[TunnelLayer(overhead=40)],
        )
        report = calculate_size_bounds(path)

        self.assertEqual(report.bounds.guaranteed_max, 1360)
        self.assertEqual(report.bounds.optimistic_max, 1460)
        self.assertEqual(report.bounds.proven_impossible_from, 1361)
        self.assertTrue(all(w.holds for w in report.bounds.witnesses))

        self.assertTrue(check_commitment(path, 1360).committed)
        self.assertFalse(check_commitment(path, 1361).committed)

    def test_df_flag_changes_fragmentation_outcome(self):
        df_path = PathSpec(
            path_id="df",
            hops=[1500],
            layers=[TunnelLayer(overhead=40)],
            dont_fragment=True,
        )
        self.assertEqual(check_commitment(df_path, 1461).status, "oversize")

        non_df = PathSpec(
            path_id="fragile",
            hops=[1500],
            layers=[TunnelLayer(overhead=40)],
            dont_fragment=False,
        )
        report = check_commitment(non_df, 1461)
        self.assertTrue(report.committed)
        self.assertEqual(report.status, "fragmented")
        self.assertTrue(report.fragmentation.feasible)
        self.assertEqual(report.fragmentation.fragment_count, 2)
        self.assertEqual(report.fragmentation.fragment_sizes[0], 1500)
        self.assertLessEqual(max(report.fragmentation.fragment_sizes), 1500)

    def test_verify_bounds_recomputes_every_boundary_witness(self):
        path = PathSpec(
            path_id="verifiable",
            hops=[("a", (1450, 1500)), ("b", 1400)],
            layers=[TunnelLayer(StepOverhead([(0, 20), (120, 70)]))],
            alignment=8,
        )
        report = calculate_size_bounds(path)
        witnesses = verify_bounds(path, report)
        self.assertGreater(len(witnesses), 0)
        self.assertTrue(all(witness.holds for witness in witnesses))

    def test_empty_payload_can_still_exceed_tiny_mtu(self):
        path = PathSpec(path_id="too-tiny", hops=[40], layers=[TunnelLayer(overhead=41)])
        size_report = calculate_size_bounds(path)
        self.assertIsNone(maximum_safe_payload(path))
        self.assertEqual(size_report.status, "no_feasible_payload")

        report = check_commitment(path, 0)
        self.assertEqual(report.status, "oversize")
        self.assertEqual(report.frame_size, 41)
        self.assertEqual(report.blocker.excess, 1)

    def test_invalid_interval_and_size_are_rejected(self):
        with self.assertRaises(Exception):
            MtuInterval(1500, 1400)
        with self.assertRaises(Exception):
            Alignment(0)
        with self.assertRaises(Exception):
            PathSpec(path_id="empty", hops=[])


if __name__ == "__main__":
    unittest.main()
