import random
import unittest

from mtu_geometry import PathSpec, StepOverhead, TunnelLayer, calculate_size_bounds


class ExhaustiveBoundaryTests(unittest.TestCase):
    def test_step_geometry_matches_brute_force_for_seeded_cases(self):
        rng = random.Random(20261127)
        for case in range(120):
            mtu = rng.randint(80, 400)
            tier_count = rng.randint(1, 5)
            starts = [0]
            while len(starts) < tier_count:
                candidate = rng.randint(1, mtu)
                if candidate not in starts:
                    starts.append(candidate)
            starts.sort()

            overhead_values = [rng.randint(0, 30)]
            for _ in starts[1:]:
                overhead_values.append(overhead_values[-1] + rng.randint(0, 25))
            tiers = list(zip(starts, overhead_values))
            path = PathSpec(
                path_id=f"random-{case}",
                hops=[mtu],
                layers=[TunnelLayer(StepOverhead(tiers))],
            )

            reported = calculate_size_bounds(path).bounds.guaranteed_max

            brute = None
            for payload in range(mtu + 1):
                if path.frame_size(payload) <= mtu:
                    brute = payload
                else:
                    # Monotone model: once failed, all later values fail.
                    break
            self.assertEqual(reported, brute, msg=f"tiers={tiers}, mtu={mtu}")
            if reported is not None:
                self.assertLessEqual(path.frame_size(reported), mtu)
                self.assertGreater(path.frame_size(reported + 1), mtu)

    def test_multihop_minimum_and_witnesses_are_exhaustively_checked(self):
        path = PathSpec(
            path_id="exhaustive-hops",
            hops=[120, 100, 130],
            layers=[TunnelLayer(StepOverhead([(0, 4), (30, 18), (70, 35)]))],
        )
        report = calculate_size_bounds(path)
        maximum = report.bounds.guaranteed_max
        self.assertIsNotNone(maximum)

        for payload in range(maximum + 1):
            frame = path.frame_size(payload)
            for hop_report in report.hops:
                if payload <= (hop_report.max_payload or -1):
                    self.assertLessEqual(frame, hop_report.safe_mtu)

        self.assertLessEqual(path.frame_size(maximum), min(120, 100, 130))
        self.assertGreater(path.frame_size(maximum + 1), 100)
        self.assertEqual(report.bottleneck.hop_id, "hop-2")


if __name__ == "__main__":
    unittest.main()
